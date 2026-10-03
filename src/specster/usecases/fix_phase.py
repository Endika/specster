"""The fix phase: apply a pull request's open review threads as commits on its branch."""

import dataclasses
import functools
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from specster.agent import (
    NO_SUBMISSION,
    TIME_UP,
    AgentError,
    Deadline,
    FixOutcome,
    Metered,
    check_fix,
    run_fix_planner,
)
from specster.approved import BuildRefused
from specster.browser import Installer, install
from specster.build import (
    BuildReport,
    BuildSetup,
    bill_fatal,
    budget_cut,
    budget_stop,
    run_build,
)
from specster.config import ModelConfig
from specster.event import Trigger
from specster.git import Author, Git, GitError, noreply_email
from specster.github import GitHubError, PullInfo, PullTracker
from specster.ledger import Ledger
from specster.llm.base import ChatModel
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics
from specster.prompts import context_block, fix_planner_prompt
from specster.render import (
    FixRow,
    FixView,
    hint,
    render_fix,
    reply_applied,
    reply_declined,
    reply_unapplied,
)
from specster.render.fix import FixStatus, RowState
from specster.repomap import RepoMap, build_repo_map
from specster.review_input import ReviewItem, ReviewReading, answered_marker, read_review
from specster.sandbox import Identity, Sandbox, SandboxError, lock_down, scratch_dir
from specster.schemas import EvidencePage, EvidenceRequest, PlanTask
from specster.skills import Fetch, SkillBook
from specster.usecases.build_phase import give_back, report_facts, role_models, skill_facts
from specster.usecases.context import (
    LABEL_COLORS,
    NO_CHECKOUT,
    PROVIDER_HINT,
    Failure,
    Outcome,
    RunContext,
    describe,
    load_phase_skills,
    log,
    usage_fields,
)
from specster.usecases.pull_request import check_host, known_spend
from specster.workspace import Workspace

PLANNER = "planner"
_NOT_PUSHED: dict[str, tuple[FixStatus, Outcome]] = {
    "not_approved": ("not_approved", "not_approved"),
    "failed": ("failed", "build_failed"),
    "budget_exhausted": ("budget", "budget_exhausted"),
}


@dataclass(frozen=True)
class _ReviewPlan:
    """The build's plan: the planner's tasks, with the review they apply as its text."""

    tasks: list[PlanTask]
    text: str
    evidence: tuple[EvidenceRequest, ...] = ()
    pages: tuple[EvidencePage, ...] = ()


@dataclass
class _Planned:
    """What the fix planner decided, and what reading the head left out."""

    fix: FixOutcome | None
    files_read: list[str]
    truncations: list[str]
    # Why the planner stopped short: a budget cap or the time limit.
    budget: str = ""
    out_of_time: bool = False


@dataclass
class _Tools:
    make_worker: Callable[[], ChatModel]
    make_reviewer: Callable[[], ChatModel]
    make_escalation: Callable[[], ChatModel] | None
    planner: ChatModel
    books: list[SkillBook]


class FixPhase:
    """`ai-fix` on an open, ready pull request from this repository."""

    def __init__(
        self,
        run: RunContext,
        trigger: Trigger,
        pull: PullInfo,
        pulls: PullTracker,
        make_model: Callable[[ModelConfig], ChatModel],
        fetch: Fetch,
        identity: Callable[[int], Identity | None],
        install_browser: Installer = install,
    ) -> None:
        self.run = run
        self.trigger = trigger
        self.pull = pull
        self.pulls = pulls
        self.make_model = make_model
        self.fetch = fetch
        self.identity = identity
        self.install_browser = install_browser
        self.lang = run.cfg.persona.language
        self.label = run.cfg.labels.fix
        self.login: str | None = None
        self.warnings: list[str] = []
        # Set once the checkout's git is in use, so cleanup knows there is a .git to tidy.
        self.git: Git | None = None

    def execute(self) -> int:
        run = self.run
        labels = run.cfg.labels
        run.tracker.ensure_labels(
            {labels.fix: LABEL_COLORS["fix"], labels.needs_human: LABEL_COLORS["needs_human"]}
        )
        known = known_spend(run)
        if known is None:
            return 0
        reading = self._read()
        if not reading.items:
            return run.refuse(
                BuildRefused(
                    _nothing(reading), hint(self.lang, "hint_fix_nothing", label=self.label)
                )
            )
        check_host(run, self.identity)
        tools = self._tools()
        scratch = scratch_dir()
        try:
            return self._run(self._git(scratch), scratch, tools, reading, known)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            if self.git is not None:
                try:
                    self.git.run("worktree", "prune")
                except GitError as e:
                    log(f"could not prune worktrees: {e}")
                give_back(run.env.workspace)

    def _read(self) -> ReviewReading:
        """The open review a trusted person wrote before the label, sanitized and tagged."""
        run, n = self.run, self.run.number
        cfg, tracker = run.cfg, run.tracker
        self.login = cfg.identity.bot_login or tracker.own_login()
        label_at = None
        if self.trigger.kind == "pr_labeled" and cfg.trust.snapshot_at_label:
            label_at = tracker.label_applied_at(n, self.label)
        snapshot = label_at or datetime.now(UTC)
        try:
            threads = self.pulls.review_threads(n)
            reviews = self.pulls.reviews(n)
        except (GitHubError, httpx.HTTPError) as e:
            raise Failure(
                f"could not read the review of pull request #{n}: {describe(e)}",
                hint(self.lang, "hint_pull_read", label=self.label),
            ) from e
        return read_review(
            self.pull,
            threads,
            reviews,
            tracker.list_comments(n),
            cfg.trust,
            snapshot,
            secrets.token_hex(8),
            login=self.login,
        )

    def _tools(self) -> _Tools:
        run = self.run
        make_worker, make_reviewer, make_escalation = role_models(run, self.make_model)
        try:
            planner = self.make_model(run.cfg.models.planner)
        except ProviderConfigError as e:
            raise Failure(describe(e), PROVIDER_HINT) from e
        build_skills = load_phase_skills(run, self.fetch, "build")
        review_skills = load_phase_skills(run, self.fetch, "review")
        run.warnings += build_skills.warnings + [
            w for w in review_skills.warnings if w not in build_skills.warnings
        ]
        books = [build_skills, review_skills]
        return _Tools(make_worker, make_reviewer, make_escalation, planner, books)

    def _git(self, scratch: Path) -> Git:
        """The checkout's git with the pull request's head in it, then locked down."""
        run, pull = self.run, self.pull
        env, login = run.env, self.login
        email = noreply_email(login, run.tracker.user_id(login) if login else None)
        git = Git(env.workspace, Author(run.cfg.persona.name, email), scratch / "git-home")
        try:
            git.head()
        except GitError as e:
            raise Failure(NO_CHECKOUT, hint(self.lang, "hint_checkout")) from e
        self.git = git
        try:
            git.fetch_commits(f"{env.server_url}/{env.repo}.git", env.token, [pull.head_sha])
        except GitError as e:
            raise Failure(
                f"could not fetch the head of pull request #{pull.number}: {e}",
                hint(self.lang, "hint_pull_fetch", label=self.label),
            ) from e
        for line in lock_down(env.workspace, git, env.secrets):
            log(f"lock down: {line}")
        return git

    def _time_left(self) -> Callable[[], float]:
        run = self.run
        deadline = run.started + run.cfg.build.max_minutes * 60
        return lambda: deadline - run.timer()

    def _run(
        self, git: Git, scratch: Path, tools: _Tools, reading: ReviewReading, known: float
    ) -> int:
        run, pull = self.run, self.pull
        cfg = run.cfg
        ledger = run.ledger = Ledger(cfg.pricing)
        time_left = self._time_left()
        head = scratch / "head"
        head.mkdir()
        git.export_tree(pull.head_sha, head, scratch / "head.index")
        ws = Workspace(head, cfg.repo_map.exclude)
        repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
        planned = self._plan(tools.planner, ws, repo_map, reading, ledger, known, time_left)
        fix = planned.fix
        if fix is None:
            status: FixStatus = "time" if planned.out_of_time else "budget"
            m = self._metrics("budget_exhausted", ledger, reading, planned, tools.books, None)
            return self._finish(status, "budget_exhausted", m, reading, None, planned.budget)
        if not fix.tasks:
            self._reply(reading, fix, None)
            m = self._metrics("refused", ledger, reading, planned, tools.books, None)
            return self._finish("declined", "refused", m, reading, fix, answered=True)
        run.build_facts = skill_facts(tools.books) | {"tasks_total": len(fix.tasks)}
        report = self._build(git, scratch, tools, repo_map, reading, fix, ledger, known, time_left)
        if report.status != "approved":
            status, outcome = _NOT_PUSHED[report.status]
            if status == "budget" and report.out_of_time:
                status = "time"
            m = self._metrics(outcome, ledger, reading, planned, tools.books, report)
            return self._finish(status, outcome, m, reading, fix, report.reason, report)
        return self._push(git, ledger, reading, planned, tools, fix, report)

    def _plan(
        self,
        model: ChatModel,
        ws: Workspace,
        repo_map: RepoMap,
        reading: ReviewReading,
        ledger: Ledger,
        known: float,
        time_left: Callable[[], float],
    ) -> _Planned:
        """Ask the fix planner, which reads an export of the head, never the checkout."""
        run = self.run
        cfg, build = run.cfg, run.cfg.build
        planner = cfg.models.planner
        meter = ledger.meter(planner)
        truncations = [
            *([repo_map.truncation] if repo_map.truncation else []),
            *reading.truncations,
        ]

        def stop() -> str | None:
            return budget_stop(ledger, cfg.budget, known)

        check = functools.partial(
            check_fix,
            keys=[i.key for i in reading.items],
            allow_workflows=build.allow_workflow_changes,
            allow_config=build.allow_config_changes,
            config_path=run.env.config_path,
        )
        system = fix_planner_prompt(
            cfg.persona,
            build.allow_workflow_changes,
            build.allow_config_changes,
            run.env.config_path,
        )
        try:
            out = run_fix_planner(
                Deadline(Metered(model, meter, stop), time_left),
                system,
                context_block(repo_map, ()),
                reading.text,
                ws,
                cfg.budget.max_turns,
                check,
            )
        except AgentError as e:
            ledger.add(PLANNER, planner, e.usage, e.turns)
            read, cut = sorted(ws.files_read), truncations + ws.truncations
            if TIME_UP in str(e):
                late = f"time limit reached: build.max_minutes is {build.max_minutes}"
                return _Planned(None, read, cut, late, out_of_time=True)
            cap = budget_cut(str(e))
            if cap is None and str(e).startswith(NO_SUBMISSION):
                raise Failure(
                    f"fix planner: {e}", hint(self.lang, "hint_fix_planner", label=self.label)
                ) from e
            if cap is None:
                raise Failure(describe(e), PROVIDER_HINT) from e
            return _Planned(None, read, cut, cap)
        except BaseException as e:
            bill_fatal(ledger, PLANNER, planner, e, meter)
            raise
        finally:
            meter.close()
        ledger.add(PLANNER, planner, out.usage, out.turns)
        return _Planned(out.value, sorted(ws.files_read), truncations + ws.truncations)

    def _build(
        self,
        git: Git,
        scratch: Path,
        tools: _Tools,
        repo_map: RepoMap,
        reading: ReviewReading,
        fix: FixOutcome,
        ledger: Ledger,
        known: float,
        time_left: Callable[[], float],
    ) -> BuildReport:
        """The plan built on the pull request's head, on a branch that never leaves the runner."""
        run, pull = self.run, self.pull
        cfg, env = run.cfg, run.env
        build = cfg.build

        def make_sandbox(slot: int) -> Sandbox:
            sandbox = Sandbox(
                self.identity(slot),
                build.test_timeout_s,
                build.test_output_max_kb * 1024,
                build.test_env,
                max_file_bytes=build.test_output_max_file_mb * 1024 * 1024,
                time_left=time_left,
            )
            sandbox.lock_down(env.workspace, git, env.secrets)
            return sandbox

        build_skills, review_skills = tools.books
        try:
            return run_build(
                BuildSetup(
                    cfg,
                    _ReviewPlan(fix.tasks, _plan_text(pull, reading, fix)),
                    git,
                    make_sandbox,
                    ledger,
                    tools.make_worker,
                    tools.make_reviewer,
                    build_skills,
                    review_skills,
                    repo_map,
                    f"specster/fix-{pull.number}",
                    pull.head_sha,
                    scratch,
                    known,
                    cfg.persona,
                    time_left,
                    env.config_path,
                    tools.make_escalation,
                    self.install_browser,
                    origin="pull_request",
                )
            )
        except SandboxError as e:
            raise Failure(
                describe(e), hint(self.lang, "hint_sandbox", label=self.label), needs_human=True
            ) from e

    def _push(
        self,
        git: Git,
        ledger: Ledger,
        reading: ReviewReading,
        planned: _Planned,
        tools: _Tools,
        fix: FixOutcome,
        report: BuildReport,
    ) -> int:
        """Push the approved commits onto the pull request's branch, only where it still was."""
        run, pull = self.run, self.pull
        env = run.env
        _check_new_commits(pull, report)
        try:
            now = self.pulls.get_pull(pull.number)
        except (GitHubError, httpx.HTTPError) as e:
            raise Failure(
                f"could not read pull request #{pull.number} again before pushing: {describe(e)}",
                hint(self.lang, "hint_pull_read", label=self.label),
            ) from e
        why = _changed(pull, now)
        moved = why is None and now.head_sha != pull.head_sha
        if why is None and not moved:
            try:
                pushed = git.push_update(
                    f"{env.server_url}/{env.repo}.git",
                    report.head,
                    pull.head_ref,
                    env.token,
                    expect=pull.head_sha,
                )
            except GitError as e:
                raise Failure(
                    f"the pull request's branch could not be pushed: {e}",
                    hint(self.lang, "hint_fix_push", label=self.label),
                ) from e
            moved = not pushed
        if moved or why is not None:
            status: FixStatus = "moved" if moved else "closed"
            reason = why or (
                f"{pull.head_ref} is no longer at {pull.head_sha[:12]}: someone pushed to it, so "
                "these commits would overwrite theirs"
            )
            m = self._metrics("refused", ledger, reading, planned, tools.books, report)
            return self._finish(status, "refused", m, reading, fix, reason, report)
        self._reply(reading, fix, report)
        m = self._metrics("fix_pushed", ledger, reading, planned, tools.books, report)
        return self._finish("pushed", "fix_pushed", m, reading, fix, report=report, answered=True)

    def _reply(self, reading: ReviewReading, fix: FixOutcome, pushed: BuildReport | None) -> None:
        """Answer each thread with what became of it; a failed reply is a warning."""
        n = self.run.number
        for item in reading.items:
            if item.reply_to is None:
                continue
            state, detail = _row_state(item, fix, pushed)
            if state == "applied":
                body = reply_applied(self.lang, detail.split())
            elif state == "declined":
                body = reply_declined(self.lang, detail)
            elif state == "unapplied":
                body = reply_unapplied(self.lang)
            else:
                continue
            try:
                self.pulls.reply_to_review_comment(n, item.reply_to, body)
            except (GitHubError, httpx.HTTPError) as e:
                log(f"could not reply to review comment {item.reply_to}: {describe(e)}")
                self.warnings.append(
                    f"could not reply in the thread of {item.where}: {describe(e)}"
                )

    def _finish(
        self,
        status: FixStatus,
        outcome: Outcome,
        m: RunMetrics,
        reading: ReviewReading,
        fix: FixOutcome | None,
        reason: str = "",
        report: BuildReport | None = None,
        *,
        answered: bool = False,
    ) -> int:
        run, pull = self.run, self.pull
        base = f"{run.env.server_url}/{run.env.repo}/pull/{pull.number}"
        pushed = report if status == "pushed" else None
        rows = [] if fix is None else [_row(item, fix, pushed, base) for item in reading.items]
        view = FixView(
            status,
            pull.head_ref,
            pull.head_sha,
            report.head if status == "pushed" and report is not None else None,
            self.label,
            report,
            rows,
            reason,
            answered_marker([i.key for i in reading.items]) if answered else "",
        )
        ctx = dataclasses.replace(
            run.context(m), hidden=reading.hidden, untrusted=reading.untrusted
        )
        # A lost lease keeps the label, so taking it off and on again retries on the new head.
        remove = [] if status == "moved" else [self.label]
        run.finish(outcome, render_fix(view, ctx), remove, metrics=m)
        return 0

    def _metrics(
        self,
        outcome: Outcome,
        ledger: Ledger,
        reading: ReviewReading,
        planned: _Planned,
        books: Sequence[SkillBook],
        report: BuildReport | None,
    ) -> RunMetrics:
        run = self.run
        unpriced = [
            f"{role} model has no price: the budget caps do not count it"
            for role in ledger.unpriced()
        ]
        tasks = len(planned.fix.tasks) if planned.fix is not None else 0
        facts: dict[str, Any] = (
            report_facts(run, report, books)
            if report is not None
            else skill_facts(books) | {"tasks_total": tasks, "warnings": list(run.warnings)}
        )
        facts["warnings"] = [
            *facts["warnings"],
            *self.warnings,
            *[u for u in unpriced if u not in facts["warnings"]],
        ]
        facts["truncations"] = planned.truncations + (report.truncations if report else [])
        return run.metrics(
            outcome,
            ledger.cost(),
            role=run.cfg.models.worker,
            roles=ledger.roles(),
            turns=ledger.turns(),
            files_read=planned.files_read,
            comments_included=reading.included,
            comments_untrusted=len(reading.untrusted),
            comments_after_label=reading.after_label,
            comments_edited_after_label=reading.edited_after_label,
            hidden_removed=len(reading.hidden),
            **facts,
            **usage_fields(ledger.usage()),
        )


def _nothing(reading: ReviewReading) -> str:
    skipped = [
        (len(reading.untrusted), "from people the trust settings leave out"),
        (reading.after_label, "written after the label"),
        (reading.edited_after_label, "edited after the label"),
        (reading.answered, "already answered by Specster"),
    ]
    counted = [f"{n} {what}" for n, what in skipped if n]
    tail = f" (skipped: {'; '.join(counted)})" if counted else ""
    return (
        f"Nothing to apply: no open review thread or review summary from a trusted reviewer{tail}"
    )


def _changed(pull: PullInfo, now: PullInfo) -> str | None:
    """Why the pull request is no longer one Specster may push to, if it is not."""
    if now.state != "open":
        return f"Pull request #{pull.number} is {now.state} now"
    if not now.head_repo or now.head_repo != now.base_repo or now.head_ref != pull.head_ref:
        return f"Pull request #{pull.number} no longer comes from {pull.head_repo}:{pull.head_ref}"
    return None


def _check_new_commits(pull: PullInfo, report: BuildReport) -> None:
    """An approved build always has commits: run_build fails one whose tasks changed no files."""
    if report.commits == 0 or report.head == pull.head_sha:
        raise AssertionError(f"an approved fix has no commits on {pull.head_sha[:12]}")


def _plan_text(pull: PullInfo, reading: ReviewReading, fix: FixOutcome) -> str:
    parts = [
        f"Pull request #{pull.number} applies its open review: the tasks change what its "
        "review items ask for.",
        reading.text,
    ]
    if fix.not_applied:
        parts += ["Review items the plan leaves as they are, with why:"]
        parts += [f"- {n.id}: {' '.join(n.reason.split())}" for n in fix.not_applied]
    return "\n".join(parts)


def _commits(item: ReviewItem, fix: FixOutcome, report: BuildReport) -> list[str]:
    tasks = set(fix.addressed.get(item.key, []))
    return [c.sha for r in report.tasks if r.task.id in tasks for c in r.commits]


def _row_state(
    item: ReviewItem, fix: FixOutcome, pushed: BuildReport | None
) -> tuple[RowState, str]:
    """What became of a review item; `pushed` is the build whose commits reached the branch."""
    declined = {n.id: n.reason for n in fix.not_applied}
    if item.key in declined:
        return "declined", declined[item.key]
    if pushed is None:
        return "planned", " ".join(fix.addressed.get(item.key, []))
    shas = _commits(item, fix, pushed)
    return ("applied", " ".join(shas)) if shas else ("unapplied", "")


def _row(item: ReviewItem, fix: FixOutcome, pushed: BuildReport | None, base: str) -> FixRow:
    state, detail = _row_state(item, fix, pushed)
    return FixRow(item.author, f"{base}#{item.anchor}", item.where, state, detail)
