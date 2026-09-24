import os
import queue
import secrets
import shutil
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from specster.agent import AgentError, attached_usage
from specster.approved import ApprovedSpec
from specster.config import BudgetConfig, Config, ModelConfig, PersonaConfig
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.ledger import Ledger
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
from specster.sandbox import RunResult, Sandbox
from specster.schemas import Finding, PlanTask, ReviewResult
from specster.skills import SkillBook
from specster.worker import TaskTools, WorkerResult, run_worker
from specster.workspace import TaskWorkspace, ToolError, Workspace, has_git_component

DIFF_MAX_CHARS = 150_000
FINAL_SLOT = 0
NO_TESTS_RUN = "No tests were run: build.test_command is not set."

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
) -> list[str]:
    paths = sorted(changes)
    refused = [p for p in paths if has_git_component(p)]
    if refused:
        raise BuildConflict(f"{refused[0]}: a .git path component")
    # TaskWorkspace reads and writes through no-follow directory fds, so a symlink in the
    # integration tree can never redirect root's write.
    ws = TaskWorkspace(tree, [], paths, allow_workflows=allow_workflows)
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
) -> str | None:
    paths = apply_changes(tree, changes, originals, allow_workflows)
    sha = git.commit_paths(tree, paths, subject) if paths else None
    expected = {p for p in paths if changes[p].encode() != originals[p]}
    committed: set[str] = set()
    if sha is not None:
        listing = git.run("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha, cwd=tree)
        committed = set(listing.split("\0")) - {""}
    missing = sorted(expected - committed)
    if missing:
        raise BuildConflict(f"commit dropped paths: {', '.join(missing)}")
    return sha


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

    def _stop(self) -> str | None:
        spent = budget_stop(self.s.ledger, self.cfg.budget, self.s.prior_known_usd)
        return spent or self._time_stop()

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
        index = {"GIT_INDEX_FILE": str(enclosure / "index")}
        self.git.run("read-tree", "--end-of-options", commit, extra_env=index)
        into = index | {"GIT_WORK_TREE": str(tree)}
        self.git.run("checkout-index", "-a", "-q", extra_env=into)
        (enclosure / "index").unlink()
        local = Git(tree, Author(self.s.persona.name, BOT_EMAIL), enclosure / "git-home")
        local.run("init", "-q", "--template=", "--initial-branch=specster")
        local.run("add", "-A", "-f", "--", ".")
        local.run("commit", "-q", "--no-verify", "--allow-empty", "-m", f"specster: {commit}")
        return tree

    def _work(
        self, task: PlanTask, base: str, round_no: int, findings: Sequence[Finding]
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
        enclosure = self.s.scratch / f"w{round_no}-{task.id}"
        started = False
        try:
            sandbox = self._sandboxes[slot]
            tree = self._enclose(sandbox, enclosure, base)
            # Snapshot the task's files before the slot can touch them.
            ws = TaskWorkspace(
                tree,
                self.cfg.repo_map.exclude,
                task.files,
                allow_workflows=self.cfg.build.allow_workflow_changes,
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
            )
            user = task_block(self.s.spec.text, task, findings, secrets.token_hex(8))
            started = True
            result = run_worker(
                self.s.make_worker(),
                self.worker_system,
                self.worker_context,
                user,
                tools,
                task,
                self.s.build_skills,
                build.max_turns_per_task,
                round_no > 0,
            )
            self.s.ledger.add("worker", self.cfg.models.worker, result.usage, result.turns)
            return result
        except BaseException as e:
            # A worker already picked from the queue must not start after a fatal error.
            self._aborted.set()
            if started:
                self._bill_fatal("worker", self.cfg.models.worker, e)
            raise
        finally:
            shutil.rmtree(enclosure, ignore_errors=True)
            with self._lock:
                self._active -= 1
            self._free.put(slot)

    def _bill_fatal(self, role: str, model: ModelConfig, e: BaseException) -> None:
        found = attached_usage(e)
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
            return
        sha = commit_changes(
            self.git,
            self.integration,
            result.changes,
            result.originals,
            result.subject,
            self.cfg.build.allow_workflow_changes,
        )
        if sha is None:
            self.warnings.append(f"{task.id} changed no files")
        else:
            record.commits.append(Commit(sha, result.subject))
        record.status = "done"
        if not record.summary:
            record.summary = result.summary

    def _run_group(
        self, group: Sequence[PlanTask], round_no: int, findings: Mapping[str, Sequence[Finding]]
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
        pool = ThreadPoolExecutor(max_workers=self.cfg.build.max_parallel)
        try:
            futures = [
                pool.submit(self._work, t, base, round_no, findings.get(t.id, ())) for t in runnable
            ]
            results = [f.result() for f in futures]
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown()
        for t, result in zip(runnable, results, strict=True):
            self._record(t, result)

    def _run_tasks(
        self, tasks: Sequence[PlanTask], round_no: int, findings: Mapping[str, Sequence[Finding]]
    ) -> None:
        depth = levels(self.tasks)
        for level in sorted({depth[t.id] for t in tasks}):
            self._run_group([t for t in tasks if depth[t.id] == level], round_no, findings)

    def _unfinished(self) -> tuple[Outcome, str] | None:
        records = [self.records[t.id] for t in self.tasks]
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

    def _final_tests(self, round_no: int) -> tuple[RunResult, bool]:
        """The integrated branch's test result and whether it is the failed setup's."""
        if self._final is None:
            self._final = self.s.make_sandbox(FINAL_SLOT)
        sandbox = self._final
        build = self.cfg.build
        assert build.test_command is not None
        head = self.git.head(self.integration)
        enclosure = self.s.scratch / f"final{round_no}"
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

    def _review(self, tests: str) -> ReviewResult:
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
        try:
            outcome = run_review(
                s.make_reviewer(),
                reviewer_system_prompt(s.persona, s.review_skills.on_demand, has_tests),
                context_block(s.repo_map, s.review_skills.inline),
                user,
                Workspace(self.integration, self.cfg.repo_map.exclude),
                s.review_skills,
                [t.id for t in self.tasks],
                self.cfg.budget.max_turns,
            )
        except AgentError as e:
            s.ledger.add("reviewer", self.cfg.models.reviewer, e.usage, e.turns)
            raise
        except BaseException as e:
            self._bill_fatal("reviewer", self.cfg.models.reviewer, e)
            raise
        s.ledger.add("reviewer", self.cfg.models.reviewer, outcome.usage, outcome.turns)
        return outcome.result

    def run(self) -> BuildReport:
        try:
            self.git.add_worktree(self.integration, self.s.base, branch=self.s.branch)
            return self._run()
        finally:
            try:
                self.git.drop_worktree(self.integration)
            except GitError as e:
                # Never in place of the build's own result or error: the branch ref is intact.
                print(f"specster: could not drop the integration worktree: {e}", file=sys.stderr)

    def _run(self) -> BuildReport:
        final: RunResult | None = None
        review: ReviewResult | None = None
        rounds = 0

        def report(status: Outcome, reason: str) -> BuildReport:
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
            )

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
                review = self._review(_test_text(final, setup_failed))
            except AgentError as e:
                return report("failed", f"reviewer: {e}")
            blocked = blocking(review)
            tests_ok = final is None or final.ok
            if not blocked and tests_ok:
                return report("approved", "")
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
            self._run_tasks([t for t in self.tasks if t.id in named], round_no + 1, named)
            stopped = self._unfinished_or_late()
            if stopped is not None:
                return report(*stopped)
        raise AssertionError("unreachable: the last round always returns")


def run_build(setup: BuildSetup) -> BuildReport:
    return _Build(setup).run()
