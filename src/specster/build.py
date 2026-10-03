import contextvars
import json
import math
import os
import queue
import secrets
import shutil
import stat
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import httpx

from specster.agent import TIME_UP, AgentError, BudgetSpent, Metered, attached_usage
from specster.approved import ApprovedSpec
from specster.browser import INSTALL_MAX_S, SCRIPT, BrowserEnv, Installer, install
from specster.config import BudgetConfig, Config, ModelConfig, PersonaConfig, PreviewConfig
from specster.evidence import (
    LOG_TAIL_CHARS,
    PAGE_MAX_HEIGHT,
    PAGE_MAX_S,
    EvidenceRun,
    Shot,
    Side,
    collect,
    read_shots,
)
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.ledger import Ledger, Meter
from specster.llm.base import ChatModel, Usage
from specster.plan import levels
from specster.prompts import (
    context_block,
    review_block,
    reviewer_system_prompt,
    task_block,
    worker_system_prompt,
)
from specster.repomap import RepoMap
from specster.review import blocking, run_review
from specster.sandbox import RunResult, Sandbox, Server
from specster.schemas import EvidencePage, Finding, PlanTask, ReviewResult
from specster.skills import SkillBook
from specster.telemetry import span
from specster.worker import STOPPED, TaskTools, WorkerResult, run_worker
from specster.workspace import CONFIG_PATH, TaskWorkspace, ToolError, Workspace, has_git_component

DIFF_MAX_CHARS = 150_000
MISE = Path("/usr/local/bin/mise")
# Tools whose own version files (.nvmrc, .ruby-version, ...) count as declared; Python is left
# out because the image's Python and uv already serve it.
_IDIOMATIC_TOOLS = "node,ruby,java,go,bun,deno,erlang,elixir"
# The files mise reads a toolchain from at the repository root: only these make a build install.
TOOL_FILES = (
    "mise.toml",
    ".mise.toml",
    ".config/mise.toml",
    ".tool-versions",
    ".nvmrc",
    ".node-version",
    ".ruby-version",
    ".java-version",
    ".go-version",
    ".bun-version",
)
# asdf and vfox plugins are scripts from anywhere; the core tools and registry backends are not.
_DISABLED_BACKENDS = "asdf,vfox"
FINAL_SLOT = 0
NO_TESTS_RUN = "No tests were run: build.test_command is not set."
# How run_loop names a loop cut by the budget, ahead of the cap's own text.
BUDGET_CUT = f"{BudgetSpent.__name__}: "

Status = Literal["done", "failed", "skipped", "not_started", "pending"]
Outcome = Literal["approved", "not_approved", "failed", "budget_exhausted"]


class BuildConflict(Exception):
    """A change no longer applies onto the integration tree: a Specster bug, never a task's."""


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


@dataclass
class TaskRecord:
    task: PlanTask
    status: Status = "pending"
    reason: str = ""
    summary: str = ""
    commits: list[Commit] = field(default_factory=list)
    last_tests: RunResult | None = None
    escalated_to: str | None = None


@dataclass
class BuildReport:
    status: Outcome
    reason: str
    tasks: list[TaskRecord]
    base: str
    head: str
    final_tests: RunResult | None
    has_tests: bool
    review: ReviewResult | None
    pending: list[Finding]
    minor: list[Finding]
    review_rounds: int
    test_runs: int
    parallel_used: int
    truncations: list[str]
    warnings: list[str]
    out_of_time: bool = False
    evidence: EvidenceRun | None = None

    @property
    def commits(self) -> int:
        return sum(len(r.commits) for r in self.tasks)


@dataclass(frozen=True)
class BuildSetup:
    cfg: Config
    spec: ApprovedSpec
    git: Git
    # Slot -> its sandbox: 1..max_parallel for workers, 0 for the final tests.
    make_sandbox: Callable[[int], Sandbox]
    ledger: Ledger
    make_worker: Callable[[], ChatModel]
    make_reviewer: Callable[[], ChatModel]
    build_skills: SkillBook
    review_skills: SkillBook
    repo_map: RepoMap
    branch: str
    base: str
    scratch: Path
    prior_known_usd: float
    persona: PersonaConfig
    # Seconds left before build.max_minutes; None never stops the build on time.
    time_left: Callable[[], float] | None = None
    config_path: str = CONFIG_PATH
    make_escalation: Callable[[], ChatModel] | None = None
    install_browser: Installer = install


def budget_stop(ledger: Ledger, budget: BudgetConfig, prior_known_usd: float) -> str | None:
    spent = ledger.known_cost()
    cap = budget.max_usd_per_build
    if cap is not None and spent >= cap:
        return f"build budget spent: ${spent:.2f} of ${cap:.2f}"
    cap = budget.max_usd_per_issue
    if cap is not None and prior_known_usd + spent >= cap:
        return f"issue budget spent: ${prior_known_usd + spent:.2f} of ${cap:.2f}"
    return None


def apply_changes(
    tree: Path,
    changes: Mapping[str, str],
    originals: Mapping[str, bytes | None],
    allow_workflows: bool = False,
    allow_config: bool = False,
    config_path: str = CONFIG_PATH,
) -> list[str]:
    paths = sorted(changes)
    refused = [p for p in paths if has_git_component(p)]
    if refused:
        raise BuildConflict(f"{refused[0]}: a .git path component")
    # TaskWorkspace reads and writes through no-follow directory fds, so a symlink in the
    # integration tree can never redirect root's write.
    ws = TaskWorkspace(
        tree,
        [],
        paths,
        allow_workflows=allow_workflows,
        allow_config=allow_config,
        config_path=config_path,
    )
    for rel in paths:
        if rel not in originals:
            raise BuildConflict(f"{rel}: changed without an original to check against")
        try:
            current = ws.original(rel)
        except ToolError as e:
            raise BuildConflict(str(e)) from e
        if current != originals[rel]:
            raise BuildConflict(f"{rel}: changed on the branch since the task's tree was made")
    for rel in paths:
        try:
            ws.write_file(rel, changes[rel])
        except ToolError as e:
            raise BuildConflict(str(e)) from e
    return paths


def commit_changes(
    git: Git,
    tree: Path,
    changes: Mapping[str, str],
    originals: Mapping[str, bytes | None],
    subject: str,
    allow_workflows: bool = False,
    allow_config: bool = False,
    config_path: str = CONFIG_PATH,
) -> str | None:
    paths = apply_changes(tree, changes, originals, allow_workflows, allow_config, config_path)
    sha = git.commit_paths(tree, paths, subject) if paths else None
    expected = {p for p in paths if changes[p].encode() != originals[p]}
    committed: set[str] = set()
    if sha is not None:
        committed = git.files_in(sha, tree)
    missing = sorted(expected - committed)
    if missing:
        raise BuildConflict(f"commit dropped paths: {', '.join(missing)}")
    return sha


def _freeze(root: Path) -> None:
    """Hand the installed toolchains to root, read-only: no slot may change what others run.

    Setuid and setgid bits go too, since a slot could set them on its own file before the
    ownership moves to root.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [".", *dirnames, *filenames]:
            path = Path(dirpath) / name if name != "." else Path(dirpath)
            os.chown(path, 0, 0, follow_symlinks=False)
            st = path.lstat()
            if not stat.S_ISLNK(st.st_mode):
                path.chmod(stat.S_IMODE(st.st_mode) & ~0o7022)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _test_text(res: RunResult | None, setup_failed: bool) -> str:
    if res is None:
        return NO_TESTS_RUN
    timed_out = " (timed out)" if res.timed_out else ""
    note = f"[{res.truncation}]\n" if res.truncation else ""
    head = "The setup command failed, so the tests did not run.\n" if setup_failed else ""
    return f"{head}exit {res.exit_code}{timed_out}\n{note}{res.output}"


class _Build:
    def __init__(self, setup: BuildSetup) -> None:
        self.s = setup
        self.cfg = setup.cfg
        self.git = setup.git
        self.tasks = setup.spec.tasks
        self.integration = setup.scratch / "branch"
        self.records = {t.id: TaskRecord(t) for t in self.tasks}
        self.truncations: list[str] = []
        self.warnings: list[str] = []
        self.test_runs = 0
        self.parallel_used = 0
        self._active = 0
        self._lock = threading.Lock()
        slots = range(1, self.cfg.build.max_parallel + 1)
        self._sandboxes = {slot: setup.make_sandbox(slot) for slot in slots}
        self._free: queue.Queue[int] = queue.Queue()
        for slot in slots:
            self._free.put(slot)
        self._final: Sandbox | None = None
        self._aborted = threading.Event()
        self._corrected: Counter[str] = Counter()
        self._out_of_time = False
        has_tests = self.cfg.build.test_command is not None
        self.worker_system = worker_system_prompt(
            setup.persona, setup.build_skills.on_demand, has_tests
        )
        self.worker_context = context_block(setup.repo_map, setup.build_skills.inline)

    def _time_stop(self) -> str | None:
        left = self.s.time_left
        if left is None or left() > 0:
            return None
        self._out_of_time = True
        return f"build time limit reached: build.max_minutes is {self.cfg.build.max_minutes}"

    def _budget_stop(self) -> str | None:
        return budget_stop(self.s.ledger, self.cfg.budget, self.s.prior_known_usd)

    def _stop(self) -> str | None:
        return self._budget_stop() or self._time_stop()

    def _unfinished_or_late(self) -> tuple[Outcome, str] | None:
        """Why the tasks did not all finish; the time limit first, since it cut them short."""
        stopped = self._unfinished()
        if stopped is None:
            return None
        late = self._time_stop()
        return ("budget_exhausted", late) if late is not None else stopped

    def _enclose(self, sandbox: Sandbox, enclosure: Path, commit: str) -> Path:
        """A credential-less repo, never a worktree: once handed over, root never runs git in it."""
        enclosure.mkdir(mode=0o750)
        enclosure.chmod(0o750)
        if sandbox.identity is not None:
            os.chown(enclosure, -1, sandbox.identity.gid, follow_symlinks=False)
        tree = enclosure / "tree"
        tree.mkdir()
        self.git.export_tree(commit, tree, enclosure / "index")
        Git(tree, Author(self.s.persona.name, BOT_EMAIL), enclosure / "git-home").seed(
            f"specster: {commit}"
        )
        return tree

    def _work(
        self,
        task: PlanTask,
        base: str,
        round_no: int,
        findings: Sequence[Finding],
        escalated: bool = False,
    ) -> WorkerResult:
        attributes = {
            "specster.task.id": task.id,
            "specster.task.round": round_no,
            "specster.task.escalated": escalated,
        }
        with span(f"task {task.id}", attributes) as current:
            result = self._attempt(task, base, round_no, findings, escalated)
            current.set_attribute("specster.task.status", result.status)
            return result

    def _attempt(
        self,
        task: PlanTask,
        base: str,
        round_no: int,
        findings: Sequence[Finding],
        escalated: bool,
    ) -> WorkerResult:
        stop = "the build was aborted" if self._aborted.is_set() else self._stop()
        if stop is not None:
            return WorkerResult(
                task.id, "not_started", stop, "", "", {}, {}, Usage(), 0, 0, None, []
            )
        slot = self._free.get()
        with self._lock:
            self._active += 1
            self.parallel_used = max(self.parallel_used, self._active)
        enclosure = self.s.scratch / f"w{round_no}-{task.id}{'-escalated' if escalated else ''}"
        role, model = self._worker_role(escalated)
        started = False
        meter: Meter | None = None
        try:
            sandbox = self._sandboxes[slot]
            tree = self._enclose(sandbox, enclosure, base)
            # Snapshot the task's files before the slot can touch them.
            ws = TaskWorkspace(
                tree,
                self.cfg.repo_map.exclude,
                task.files,
                allow_workflows=self.cfg.build.allow_workflow_changes,
                allow_config=self.cfg.build.allow_config_changes,
                config_path=self.s.config_path,
            )
            sandbox.hand_over(tree)
            home = sandbox.new_home(enclosure, "home")
            build = self.cfg.build
            tools = TaskTools(
                ws,
                sandbox,
                home,
                build.setup_command,
                build.test_command,
                task.id,
                self.s.time_left,
                self._aborted.is_set,
            )
            user = task_block(self.s.spec.text, task, findings, secrets.token_hex(8))
            started = True
            make = self.s.make_escalation if escalated else self.s.make_worker
            assert make is not None
            meter = self.s.ledger.meter(model)
            result = run_worker(
                Metered(make(), meter, self._budget_stop),
                self.worker_system,
                self.worker_context,
                user,
                tools,
                task,
                self.s.build_skills,
                build.max_turns_per_task,
                round_no > 0,
            )
            self.s.ledger.add(role, model, result.usage, result.turns)
            return result
        except BaseException as e:
            # A worker already picked from the queue must not start after a fatal error.
            self._aborted.set()
            if started:
                self._bill_fatal(role, model, e, meter)
            raise
        finally:
            if meter is not None:
                meter.close()
            shutil.rmtree(enclosure, ignore_errors=True)
            with self._lock:
                self._active -= 1
            self._free.put(slot)

    def _worker_role(self, escalated: bool) -> tuple[str, ModelConfig]:
        models = self.cfg.models
        if escalated and models.escalation is not None:
            return "worker-escalated", models.escalation
        return "worker", models.worker

    def _should_escalate(self, result: WorkerResult) -> bool:
        """A task that failed on its own, not one stopped by time, budget or another's error."""
        if self.s.make_escalation is None or self.cfg.models.escalation is None:
            return False
        cut = (TIME_UP, STOPPED, BUDGET_CUT)
        if result.status != "failed" or any(why in result.reason for why in cut):
            return False
        stop = self._stop()
        if stop is not None:
            self.warnings.append(f"{result.task_id} was not escalated: {stop}")
            return False
        return True

    def _bill_fatal(
        self, role: str, model: ModelConfig, e: BaseException, meter: Meter | None
    ) -> None:
        found = attached_usage(e)
        if found is None and meter is not None:
            usage, turns = meter.snapshot()
            # A done turn is known (AgentError's turn_no - 1); with zero, a send may be in flight.
            found = (usage, turns) if turns else None
        if found is None:
            # Turns may have been paid for; an unknown cost is never billed as $0.
            self.s.ledger.mark_unknown(role, model)
        else:
            self.s.ledger.add(role, model, *found)

    def _record(self, task: PlanTask, result: WorkerResult) -> None:
        record = self.records[task.id]
        self.test_runs += result.test_runs
        self.truncations.extend(result.truncations)
        if result.status != "done":
            record.status, record.reason = result.status, result.reason
            record.last_tests = result.last_tests
            return
        sha = commit_changes(
            self.git,
            self.integration,
            result.changes,
            result.originals,
            result.subject,
            self.cfg.build.allow_workflow_changes,
            self.cfg.build.allow_config_changes,
            self.s.config_path,
        )
        if sha is None:
            self.warnings.append(f"{task.id} changed no files")
        else:
            record.commits.append(Commit(sha, result.subject))
        record.status = "done"
        if not record.summary:
            record.summary = result.summary

    def _run_group(
        self,
        group: Sequence[PlanTask],
        round_no: int,
        findings: Mapping[str, Sequence[Finding]],
        escalate: frozenset[str] = frozenset(),
    ) -> None:
        base = self.git.head(self.integration)
        runnable: list[PlanTask] = []
        for t in group:
            blocked = [d for d in t.depends_on if self.records[d].status != "done"]
            if blocked:
                self.records[t.id].status = "skipped"
                self.records[t.id].reason = f"depends on {', '.join(blocked)}, which did not finish"
            else:
                runnable.append(t)
        results = self._pool(runnable, base, round_no, findings, escalate)
        retry = [
            t
            for t, r in zip(runnable, results, strict=True)
            if t.id not in escalate and self._should_escalate(r)
        ]
        # A failed task tries once more from the same base, with the stronger model.
        again = dict(
            zip(
                [t.id for t in retry],
                self._pool(retry, base, round_no, findings, frozenset(t.id for t in retry)),
                strict=True,
            )
        )
        model = self.cfg.models.escalation
        for t, result in zip(runnable, results, strict=True):
            if model is not None and (t.id in escalate or t.id in again):
                self.records[t.id].escalated_to = model.model
            self._record(t, again.get(t.id, result))

    def _pool(
        self,
        tasks: Sequence[PlanTask],
        base: str,
        round_no: int,
        findings: Mapping[str, Sequence[Finding]],
        escalate: frozenset[str],
    ) -> list[WorkerResult]:
        if not tasks:
            return []
        pool = ThreadPoolExecutor(max_workers=self.cfg.build.max_parallel)
        try:
            # One context per task: the task spans hang off the current one, and a context
            # cannot be entered by two threads at once.
            futures = [
                pool.submit(
                    contextvars.copy_context().run,
                    self._work,
                    t,
                    base,
                    round_no,
                    findings.get(t.id, ()),
                    t.id in escalate,
                )
                for t in tasks
            ]
            results = [f.result() for f in futures]
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown()
        return results

    def _run_tasks(
        self,
        tasks: Sequence[PlanTask],
        round_no: int,
        findings: Mapping[str, Sequence[Finding]],
        escalate: frozenset[str] = frozenset(),
    ) -> None:
        depth = levels(self.tasks)
        for level in sorted({depth[t.id] for t in tasks}):
            group = [t for t in tasks if depth[t.id] == level]
            self._run_group(group, round_no, findings, escalate)

    def _unfinished(self) -> tuple[Outcome, str] | None:
        records = [self.records[t.id] for t in self.tasks]
        for r in records:
            cap = _budget_cut(r.reason) if r.status == "failed" else None
            if cap is not None:
                return "budget_exhausted", f"{r.task.id}: {cap}"
        order: tuple[tuple[Status, Outcome], ...] = (
            ("not_started", "budget_exhausted"),
            ("failed", "failed"),
            ("skipped", "failed"),
        )
        for status, outcome in order:
            for r in records:
                if r.status == status:
                    return outcome, f"{r.task.id}: {r.reason}"
        return None

    def _final_tests(self, round_no: int | None) -> tuple[RunResult, bool]:
        """The integration branch's test result, at the base commit when round_no is None, and
        whether it is the failed setup's."""
        with span("final_tests", {} if round_no is None else {"specster.round": round_no}):
            return self._run_final_tests("base" if round_no is None else str(round_no))

    def _run_final_tests(self, tag: str) -> tuple[RunResult, bool]:
        if self._final is None:
            self._final = self.s.make_sandbox(FINAL_SLOT)
        sandbox = self._final
        build = self.cfg.build
        assert build.test_command is not None
        head = self.git.head(self.integration)
        enclosure = self.s.scratch / f"final-{tag}"
        try:
            tree = self._enclose(sandbox, enclosure, head)
            sandbox.hand_over(tree)
            home = sandbox.new_home(enclosure, "home")
            if build.setup_command is not None:
                res = sandbox.run(build.setup_command, tree, home, "setup final")
                if res.truncation:
                    self.truncations.append(res.truncation)
                if not res.ok:
                    return res, True
            res = sandbox.run(build.test_command, tree, home, "tests final")
            self.test_runs += 1
            if res.truncation:
                self.truncations.append(res.truncation)
            return res, False
        finally:
            shutil.rmtree(enclosure, ignore_errors=True)

    def _install_tools(self) -> RunResult | None:
        """Install the declared toolchains once, as the final slot; the failed run if any."""
        build = self.cfg.build
        if not build.tools and not self.git.present(self.s.base, TOOL_FILES):
            return None
        if not MISE.exists():
            self.warnings.append(f"toolchains are declared, but {MISE} is missing: none installed")
            return None
        if self._final is None:
            self._final = self.s.make_sandbox(FINAL_SLOT)
        sandbox = self._final
        tools = self.s.scratch / "tools"
        tools.mkdir(mode=0o755)
        if sandbox.identity is not None:
            os.chown(tools, sandbox.identity.uid, sandbox.identity.gid)
        enclosure = self.s.scratch / "tools-install"
        try:
            tree = self._enclose(sandbox, enclosure, self.s.base)
            wanted = enclosure / "tools.toml"
            wanted.write_text(
                "[tools]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in build.tools.items())
            )
            wanted.chmod(0o644)
            sandbox.hand_over(tree)
            home = sandbox.new_home(enclosure, "home")
            env = [
                f"MISE_DATA_DIR={tools}",
                f"MISE_CACHE_DIR={home}/.cache/mise",
                f"MISE_STATE_DIR={home}/.local/state/mise",
                f"MISE_GLOBAL_CONFIG_FILE={wanted}",
                f"MISE_TRUSTED_CONFIG_PATHS={tree}",
                f"MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS={_IDIOMATIC_TOOLS}",
                f"MISE_DISABLE_BACKENDS={_DISABLED_BACKENDS}",
                "MISE_YES=1",
                "MISE_QUIET=1",
            ]
            mise = ["/usr/bin/env", *env, str(MISE)]
            for step in ("install", "bin-paths"):
                res = sandbox.run([*mise, step], tree, home, f"toolchains {step}")
                if res.truncation:
                    self.truncations.append(res.truncation)
                if not res.ok:
                    return res
        finally:
            shutil.rmtree(enclosure, ignore_errors=True)
        root = tools.resolve()
        bins = [
            line
            for line in res.output.splitlines()
            if line.startswith("/") and Path(line).resolve().is_relative_to(root)
        ]
        if sandbox.identity is not None:
            _freeze(tools)
        for slot_box in [*self._sandboxes.values(), sandbox]:
            slot_box.use_tools(bins)
        return None

    def _evidence(self) -> EvidenceRun | None:
        """The approved requests at base and head; never raises, never changes the outcome."""
        with span("evidence"):
            return self._collect_evidence()

    def _collect_evidence(self) -> EvidenceRun | None:
        requests = self.s.spec.evidence
        pages = self.s.spec.pages
        preview = self.cfg.build.preview
        if not requests and not pages:
            return None
        if preview is None:
            self.warnings.append(
                "the spec lists evidence, but build.preview is not set: none collected"
            )
            return None
        if self._time_stop() is not None:
            self.warnings.append("evidence skipped: the build is out of time")
            return None
        browser = self._install_browser() if pages else None
        if pages and browser is None and not requests:
            return None
        enclosures: list[Path] = []
        failed: dict[str, str] = {}
        homes: dict[Side, Path] = {}
        try:
            if self._final is None:
                self._final = self.s.make_sandbox(FINAL_SLOT)
            sandbox = self._final

            def start_side(side: Side) -> tuple[Server | None, str | None]:
                return self._start_side(sandbox, preview, side, enclosures, failed, homes)

            def shoot(side: Side) -> tuple[dict[str, tuple[Shot, ...]], str]:
                assert browser is not None and preview is not None
                return self._shoot(sandbox, browser, preview, pages, side, homes[side])

            # The app is on loopback: an HTTP(S)_PROXY from the runner must not catch it.
            with httpx.Client(trust_env=False) as client:
                run = collect(
                    start_side,
                    preview,
                    requests,
                    client,
                    self.s.time_left or _forever,
                    pages=pages,
                    shoot=shoot if browser is not None else None,
                )
        except Exception as e:
            traceback.print_exc()
            self.warnings.append(f"evidence: {type(e).__name__}: {e}")
            return None
        finally:
            for enclosure in enclosures:
                shutil.rmtree(enclosure, ignore_errors=True)
        problems = tuple(
            replace(p, log_tail=failed[p.side][-LOG_TAIL_CHARS:]) if p.side in failed else p
            for p in run.problems
        )
        return replace(run, problems=problems, logs={**run.logs, **failed})

    def _install_browser(self) -> BrowserEnv | None:
        """Once per build, as root; a failure only costs the screenshots."""
        left = self.s.time_left() if self.s.time_left is not None else INSTALL_MAX_S
        try:
            with span("browser install"):
                got = self.s.install_browser(min(INSTALL_MAX_S, left))
        except Exception as e:
            traceback.print_exc()
            got = f"the browser install failed: {type(e).__name__}: {e}"
        if isinstance(got, str):
            self.warnings.append(f"screenshots skipped: {got}")
            return None
        return got

    def _shoot(
        self,
        sandbox: Sandbox,
        browser: BrowserEnv,
        preview: PreviewConfig,
        pages: Sequence[EvidencePage],
        side: Side,
        home: Path,
    ) -> tuple[dict[str, tuple[Shot, ...]], str]:
        """Shot as the slot while the side's server runs; the server's stop reaps the browser."""
        out = home / "shots"
        argv = [
            "/usr/bin/env",
            f"PLAYWRIGHT_BROWSERS_PATH={browser.browsers}",
            str(browser.python),
            "-I",
            str(SCRIPT),
            preview.origin,
            str(out),
            json.dumps([{"name": p.name, "path": p.path} for p in pages]),
            str(PAGE_MAX_HEIGHT),
            f"{PAGE_MAX_S:g}",
        ]
        res = sandbox.run(argv, home, home, f"browser {side}", reap=False)
        if res.truncation:
            self.truncations.append(res.truncation)
        log = res.output
        if res.timed_out:
            log += f"\nbrowser {side}: timed out\n"
        return read_shots(out, pages, side), log

    def _start_side(
        self,
        sandbox: Sandbox,
        preview: PreviewConfig,
        side: Side,
        enclosures: list[Path],
        failed: dict[str, str],
        homes: dict[Side, Path],
    ) -> tuple[Server | None, str | None]:
        """The side's server, started after the setup and seed commands; else why not."""
        commit = self.s.base if side == "base" else self.git.head(self.integration)
        enclosure = self.s.scratch / f"evidence-{side}"
        enclosures.append(enclosure)
        tree = self._enclose(sandbox, enclosure, commit)
        sandbox.hand_over(tree)
        home = homes[side] = sandbox.new_home(enclosure, "home")
        steps = [
            ("setup_command", self.cfg.build.setup_command),
            ("seed_command", preview.seed_command),
        ]
        for which, argv in steps:
            if argv is None:
                continue
            res = sandbox.run(argv, tree, home, f"{which.removesuffix('_command')} {side}")
            if res.truncation:
                self.truncations.append(res.truncation)
            if not res.ok:
                failed[side] = res.output
                timed_out = ", timed out" if res.timed_out else ""
                return None, f"{which} failed (exit {res.exit_code}{timed_out})"
        return sandbox.start(preview.serve_command, tree, home, f"serve {side}"), None

    def _review(self, tests: str, round_no: int) -> ReviewResult:
        with span("review", {"specster.round": round_no}):
            return self._run_review(tests)

    def _run_review(self, tests: str) -> ReviewResult:
        head = self.git.head(self.integration)
        diff = self.git.diff(self.s.base, head)
        note = None
        if len(diff) > DIFF_MAX_CHARS:
            note = f"diff cut to the first {DIFF_MAX_CHARS:,} of {len(diff):,} characters"
            self.truncations.append(note)
            diff = diff[:DIFF_MAX_CHARS]
        commits = [
            (t.id, c.sha[:7], c.subject) for t in self.tasks for c in self.records[t.id].commits
        ]
        s = self.s
        has_tests = self.cfg.build.test_command is not None
        user = review_block(
            s.spec.text, self.tasks, commits, diff, note, tests, secrets.token_hex(8)
        )
        meter = s.ledger.meter(self.cfg.models.reviewer)
        try:
            outcome = run_review(
                Metered(s.make_reviewer(), meter, self._budget_stop),
                reviewer_system_prompt(s.persona, s.review_skills.on_demand, has_tests),
                context_block(s.repo_map, s.review_skills.inline),
                user,
                Workspace(self.integration, self.cfg.repo_map.exclude),
                s.review_skills,
                [t.id for t in self.tasks],
                self.cfg.budget.max_turns,
                s.time_left,
            )
        except AgentError as e:
            s.ledger.add("reviewer", self.cfg.models.reviewer, e.usage, e.turns)
            raise
        except BaseException as e:
            self._bill_fatal("reviewer", self.cfg.models.reviewer, e, meter)
            raise
        else:
            s.ledger.add("reviewer", self.cfg.models.reviewer, outcome.usage, outcome.turns)
        finally:
            meter.close()
        return outcome.result

    def run(self) -> BuildReport:
        try:
            self.git.add_worktree(self.integration, self.s.base, branch=self.s.branch)
            return self._run()
        finally:
            shutil.rmtree(self.s.scratch / "tools", ignore_errors=True)
            try:
                self.git.drop_worktree(self.integration)
            except GitError as e:
                # Never in place of the build's own result or error: the branch ref is intact.
                print(f"specster: could not drop the integration worktree: {e}", file=sys.stderr)

    def _run(self) -> BuildReport:
        final: RunResult | None = None
        review: ReviewResult | None = None
        rounds = 0

        def report(
            status: Outcome, reason: str, evidence: EvidenceRun | None = None
        ) -> BuildReport:
            last = review.findings if review is not None else []
            unpriced = [
                f"{role} model has no price: the budget caps do not count it"
                for role in self.s.ledger.unpriced()
            ]
            return BuildReport(
                status,
                reason,
                [self.records[t.id] for t in self.tasks],
                self.s.base,
                self.git.head(self.integration),
                final,
                self.cfg.build.test_command is not None,
                review,
                blocking(review) if review is not None else [],
                [f for f in last if f.severity == "minor"],
                rounds,
                self.test_runs,
                self.parallel_used,
                self.truncations,
                [*self.warnings, *unpriced],
                status == "budget_exhausted" and self._out_of_time,
                evidence,
            )

        build = self.cfg.build
        previewed = build.preview is not None and bool(self.s.spec.evidence or self.s.spec.pages)
        if build.setup_command is not None or build.test_command is not None or previewed:
            failed_tools = self._install_tools()
            if failed_tools is not None:
                final = failed_tools
                return report("failed", "the toolchains could not be installed")
        if build.test_command is not None and not build.allow_failing_base:
            # A red base would fail every task's tests whatever its worker writes.
            final, setup_failed = self._final_tests(None)
            if not final.ok:
                what = "setup_command fails" if setup_failed else "tests fail"
                return report(
                    "failed",
                    f"the {what} on the base commit, before any task ran: fix that first, or set "
                    "build.allow_failing_base: true if this issue is about fixing it",
                )
            final = None
        self._run_tasks(self.tasks, 0, {})
        stopped = self._unfinished_or_late()
        if stopped is not None:
            return report(*stopped)
        if not any(r.commits for r in self.records.values()):
            return report("failed", "the tasks changed no files")
        max_rounds = self.cfg.build.max_review_rounds
        for round_no in range(max_rounds + 1):
            stop = self._stop()
            if stop is not None:
                return report("budget_exhausted", stop)
            setup_failed = False
            if self.cfg.build.test_command is not None:
                final, setup_failed = self._final_tests(round_no)
                stop = self._stop()
                if stop is not None:
                    return report("budget_exhausted", stop)
            try:
                review = self._review(_test_text(final, setup_failed), round_no)
            except AgentError as e:
                cap = _budget_cut(str(e))
                if cap is not None:
                    return report("budget_exhausted", cap)
                late = self._time_stop()
                if late is not None:
                    return report("budget_exhausted", late)
                return report("failed", f"reviewer: {e}")
            blocked = blocking(review)
            tests_ok = final is None or final.ok
            if not blocked and tests_ok:
                return report("approved", "", self._evidence())
            if not blocked:
                return report(
                    "not_approved", "tests fail on the branch and the reviewer named no task to fix"
                )
            if round_no == max_rounds:
                return report(
                    "not_approved",
                    f"{_count(len(blocked), 'blocking finding')} left after "
                    f"{_count(rounds, 'correction round')}",
                )
            rounds += 1
            named: dict[str, list[Finding]] = {}
            for f in blocked:
                named.setdefault(f.task_id, []).append(f)
            # A task the reviewer blocks again after a correction round gets the stronger model.
            again = frozenset(t for t in named if self._corrected[t] > 0)
            escalate = again if self.cfg.models.escalation is not None else frozenset()
            for t in named:
                self._corrected[t] += 1
            self._run_tasks([t for t in self.tasks if t.id in named], round_no + 1, named, escalate)
            stopped = self._unfinished_or_late()
            if stopped is not None:
                return report(*stopped)
        raise AssertionError("unreachable: the last round always returns")


def _budget_cut(reason: str) -> str | None:
    """The cap that cut a model loop short, if the budget is what stopped it."""
    _, cut, cap = reason.partition(BUDGET_CUT)
    return cap.split(";")[0] if cut else None


def _forever() -> float:
    return math.inf


def run_build(setup: BuildSetup) -> BuildReport:
    return _Build(setup).run()
