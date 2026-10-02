"""The build phase: build the approved plan, review it, open a pull request."""

import dataclasses
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from specster.approved import (
    ApprovedSpec,
    BuildRefused,
    approved_spec,
)
from specster.browser import Installer, install
from specster.build import FINAL_SLOT, BuildReport, BuildSetup, run_build
from specster.closing import rewrite_references
from specster.config import ModelConfig
from specster.event import Trigger
from specster.evidence import EvidenceRun
from specster.evidence import files as evidence_files
from specster.evidence_branch import EVIDENCE_BRANCH, EvidenceBranchError, publish
from specster.git import Author, Git, GitError, noreply_email
from specster.github import Comment, GitHubError, Issue
from specster.ledger import Ledger
from specster.llm.base import ChatModel
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics, spent
from specster.render import (
    BuildView,
    hint,
    render_budget,
    render_build,
    render_pr_body,
    spec_title,
)
from specster.repomap import RepoMap, build_repo_map
from specster.sandbox import (
    DOCKER_SOCKET,
    Identity,
    Sandbox,
    SandboxError,
    docker_socket_problem,
    lock_down,
    require_root,
    restore_owner,
    scratch_dir,
)
from specster.skills import Fetch, Skill, SkillBook
from specster.thread import previous_runs
from specster.usecases.context import (
    LABEL_COLORS,
    NO_CHECKOUT,
    Failure,
    Outcome,
    RunContext,
    describe,
    load_phase_skills,
    log,
    login_warnings_for,
    usage_fields,
)
from specster.workspace import Workspace

_BUILD_OUTCOMES: dict[str, Outcome] = {
    "approved": "pr_opened",
    "not_approved": "not_approved",
    "failed": "build_failed",
    "budget_exhausted": "budget_exhausted",
}


def _low_budget(run: RunContext, known: float) -> list[str]:
    budget = run.cfg.budget
    issue_cap, build_cap = budget.max_usd_per_issue, budget.max_usd_per_build
    if issue_cap is None or build_cap is None:
        return []
    left = issue_cap - known
    if left >= build_cap:
        return []
    return [
        f"the issue budget has ${left:.2f} left of ${issue_cap:.2f}, below "
        f"budget.max_usd_per_build: this build stops at ${left:.2f}"
    ]


def _models(
    run: RunContext, make_model: Callable[[ModelConfig], ChatModel]
) -> tuple[Callable[[], ChatModel], Callable[[], ChatModel], Callable[[], ChatModel] | None]:
    models = run.cfg.models
    roles = [("models.worker", models.worker), ("models.reviewer", models.reviewer)]
    escalation = models.escalation
    if escalation is not None:
        roles.append(("models.escalation", escalation))
    for key, cfg in roles:
        try:
            make_model(cfg)
        except ProviderConfigError as e:
            raise Failure(
                describe(e), hint(run.cfg.persona.language, "hint_role_model", key=key)
            ) from e
    stronger = (lambda: make_model(escalation)) if escalation is not None else None
    return lambda: make_model(models.worker), lambda: make_model(models.reviewer), stronger


def _skill_facts(books: Sequence[SkillBook]) -> dict[str, Any]:
    def names(skills: Sequence[Skill]) -> list[str]:
        return list(dict.fromkeys(s.name for s in skills))

    return {
        "skills_available": names([s for b in books for s in b.inline + b.on_demand]),
        "skills_inlined": names([s for b in books for s in b.inline]),
        "skills_read": sorted({n for b in books for n in b.read}),
    }


def _report_facts(
    run: RunContext, report: BuildReport, books: Sequence[SkillBook]
) -> dict[str, Any]:
    return _skill_facts(books) | {
        "tasks_total": len(report.tasks),
        "tasks_done": sum(1 for r in report.tasks if r.status == "done"),
        "tasks_escalated": sum(1 for r in report.tasks if r.escalated_to),
        "test_runs": report.test_runs,
        "parallel_used": report.parallel_used,
        "review_rounds": report.review_rounds,
        "evidence_items": len(report.evidence.items) if report.evidence else 0,
        "evidence_problems": len(report.evidence.problems) if report.evidence else 0,
        "truncations": report.truncations,
        "warnings": run.warnings + report.warnings,
    }


def _build_metrics(
    run: RunContext, ledger: Ledger, report: BuildReport, books: Sequence[SkillBook], **fields: Any
) -> RunMetrics:
    return run.metrics(
        _BUILD_OUTCOMES[report.status],
        ledger.cost(),
        role=run.cfg.models.worker,
        phase="build",
        roles=ledger.roles(),
        turns=ledger.turns(),
        **_report_facts(run, report, books),
        **usage_fields(ledger.usage()),
        **fields,
    )


def _default_tip(git: Git, default: str, event_sha: str) -> str | None:
    ref = f"refs/remotes/origin/{default}^{{commit}}"
    try:
        return git.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref).strip()
    except GitError:
        return event_sha or None


@dataclass(frozen=True)
class _Approval:
    """The plan the build may run, and what was learned deciding that it may."""

    issue: Issue
    spec: ApprovedSpec
    after: list[Comment]
    login: str | None
    default: str
    known: float


@dataclass(frozen=True)
class _Tools:
    """Everything the build needs besides the checkout: its branch, models, skills and map."""

    branch: str
    make_worker: Callable[[], ChatModel]
    make_reviewer: Callable[[], ChatModel]
    make_escalation: Callable[[], ChatModel] | None
    books: list[SkillBook]
    repo_map: RepoMap


class BuildPhase:
    """`ai-build`: build the approved plan in sandboxes, review it, open a pull request."""

    def __init__(
        self,
        run: RunContext,
        trigger: Trigger,
        make_model: Callable[[ModelConfig], ChatModel],
        fetch: Fetch,
        identity: Callable[[int], Identity | None],
        install_browser: Installer = install,
    ) -> None:
        self.run = run
        self.trigger = trigger
        self.make_model = make_model
        self.fetch = fetch
        self.identity = identity
        self.install_browser = install_browser
        self.lang = run.cfg.persona.language
        # Set once the checkout's git is in use, so cleanup knows there is a .git to tidy.
        self.git: Git | None = None

    def execute(self) -> int:
        labels = self.run.cfg.labels
        self.run.tracker.ensure_labels(
            {
                labels.build: LABEL_COLORS["build"],
                labels.built: LABEL_COLORS["built"],
                labels.needs_human: LABEL_COLORS["needs_human"],
            }
        )
        approval = self._approve()
        if isinstance(approval, int):
            return approval
        tools = self._prepare()
        scratch = scratch_dir()
        try:
            built = self._build(approval, tools, scratch)
            if isinstance(built, int):
                return built
            return self._publish(approval, tools, scratch, *built)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            if self.git is not None:
                try:
                    self.git.run("worktree", "prune")
                except GitError as e:
                    log(f"could not prune worktrees: {e}")
                _give_back(self.run.env.workspace)

    def _approve(self) -> _Approval | int:
        """The approved plan, or the exit code of a refusal or a spent issue budget."""
        run = self.run
        cfg, labels, n, tracker, env = run.cfg, run.cfg.labels, run.number, run.tracker, run.env
        issue = tracker.get_issue(n)
        comments = tracker.list_comments(n)
        own = tracker.own_login()
        login = cfg.identity.bot_login or own
        run.warnings = login_warnings_for(cfg.identity.bot_login, own)
        labeled = self.trigger.kind == "labeled"
        label_at = tracker.label_applied_at(n, labels.build) if labeled else None
        try:
            spec, after = approved_spec(
                issue,
                comments,
                cfg.trust,
                labels,
                cfg.build,
                label_at,
                tracker.body_edited_at(n),
                login=login,
                config_path=env.config_path,
            )
        except BuildRefused as r:
            return run.refuse(r)
        default = tracker.default_branch()
        if env.ref and env.ref != f"refs/heads/{default}":
            return run.refuse(
                BuildRefused(
                    f"The workflow ran on {env.ref}, not on the default branch {default}",
                    hint(self.lang, "hint_default_branch"),
                )
            )
        known, unknown = spent(previous_runs(comments, login))
        if unknown:
            run.warnings.append(
                f"{unknown} previous runs have unknown cost and are not counted in the budget"
            )
        cap = cfg.budget.max_usd_per_issue
        if cap is not None and known >= cap:
            m = run.metrics("budget_exhausted", 0.0, role=cfg.models.worker, warnings=run.warnings)
            run.finish(
                "budget_exhausted",
                render_budget(known, unknown, cap, run.context(m)),
                [labels.build],
                metrics=m,
            )
            return 0
        run.warnings += _low_budget(run, known)
        return _Approval(issue, spec, after, login, default, known)

    def _prepare(self) -> _Tools:
        """Every check that needs no checkout, then the models, skills and repository map."""
        run = self.run
        cfg, labels, env = run.cfg, run.cfg.labels, run.env
        branch = f"specster/issue-{run.number}"
        if run.tracker.branch_exists(branch):
            raise Failure(
                f"Branch {branch} already exists on the remote",
                hint(self.lang, "hint_branch_exists", label=labels.build),
            )
        try:
            require_root(self.identity(FINAL_SLOT))
        except SandboxError as e:
            raise Failure(str(e), hint(self.lang, "hint_root")) from e
        socket = docker_socket_problem(DOCKER_SOCKET)
        if socket is not None:
            raise Failure(socket, hint(self.lang, "hint_docker_socket", label=labels.build))
        make_worker, make_reviewer, make_escalation = _models(run, self.make_model)
        build_skills = load_phase_skills(run, self.fetch, "build")
        review_skills = load_phase_skills(run, self.fetch, "review")
        run.warnings += build_skills.warnings + [
            w for w in review_skills.warnings if w not in build_skills.warnings
        ]
        repo_map = build_repo_map(
            Workspace(env.workspace, cfg.repo_map.exclude), cfg.repo_map.max_tokens
        )
        return _Tools(
            branch,
            make_worker,
            make_reviewer,
            make_escalation,
            [build_skills, review_skills],
            repo_map,
        )

    def _build(
        self, approval: _Approval, tools: _Tools, scratch: Path
    ) -> tuple[Git, BuildReport, Ledger] | int:
        """Lock the checkout down and run the build, or the exit code of a stale checkout."""
        run = self.run
        cfg, labels, env = run.cfg, run.cfg.labels, run.env
        login = approval.login
        email = noreply_email(login, run.tracker.user_id(login) if login else None)
        git = self.git = Git(env.workspace, Author(cfg.persona.name, email), scratch / "git-home")
        try:
            base = git.head()
        except GitError as e:
            raise Failure(NO_CHECKOUT, hint(self.lang, "hint_checkout")) from e
        tip = _default_tip(git, approval.default, env.sha)
        if tip is not None and tip != base:
            return run.refuse(
                BuildRefused(
                    f"The checkout is at {base[:12]}, not at the tip of {approval.default} "
                    f"({tip[:12]}) that the event saw",
                    hint(self.lang, "hint_head", label=labels.build),
                )
            )
        for line in lock_down(env.workspace, git, env.secrets):
            log(f"lock down: {line}")
        build = cfg.build
        deadline = run.started + build.max_minutes * 60

        def time_left() -> float:
            return deadline - run.timer()

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

        ledger = run.ledger = Ledger(cfg.pricing)
        build_skills, review_skills = tools.books
        run.build_facts = _skill_facts(tools.books) | {"tasks_total": len(approval.spec.tasks)}
        try:
            report = run_build(
                BuildSetup(
                    cfg,
                    approval.spec,
                    git,
                    make_sandbox,
                    ledger,
                    tools.make_worker,
                    tools.make_reviewer,
                    build_skills,
                    review_skills,
                    tools.repo_map,
                    tools.branch,
                    base,
                    scratch,
                    approval.known,
                    cfg.persona,
                    time_left,
                    env.config_path,
                    tools.make_escalation,
                    self.install_browser,
                )
            )
        except SandboxError as e:
            raise Failure(
                describe(e), hint(self.lang, "hint_sandbox", label=labels.build), needs_human=True
            ) from e
        truncations = [tools.repo_map.truncation] if tools.repo_map.truncation else []
        report.truncations[:0] = truncations
        run.build_facts = _report_facts(run, report, tools.books)
        return git, report, ledger

    def _publish(
        self,
        approval: _Approval,
        tools: _Tools,
        scratch: Path,
        git: Git,
        report: BuildReport,
        ledger: Ledger,
    ) -> int:
        """Push what was committed, open the pull request if approved, and comment."""
        run = self.run
        labels, env, n, branch = run.cfg.labels, run.env, run.number, tools.branch
        spec = approval.spec
        branch_url = None
        if report.commits > 0:
            try:
                git.push(f"{env.server_url}/{env.repo}.git", branch, env.token)
            except GitError as e:
                raise Failure(
                    f"the branch could not be pushed: {e}",
                    hint(self.lang, "hint_push", label=labels.build),
                    needs_human=True,
                ) from e
            branch_url = f"{env.server_url}/{env.repo}/tree/{branch}"
        issue_url = f"{env.server_url}/{env.repo}/issues/{n}"
        view = BuildView(
            report,
            spec.text,
            f"{issue_url}#issuecomment-{spec.comment.id}",
            branch,
            branch_url,
            None,
            [(c.author, f"{issue_url}#issuecomment-{c.id}") for c in approval.after],
            n,
            run.cfg.build.close_issue,
        )
        m = _build_metrics(run, ledger, report, tools.books)
        if report.status == "approved":
            title = approval.issue.title
            try:
                pull = run.tracker.create_pull(
                    spec_title(spec.text) or rewrite_references(" ".join(title.split())),
                    render_pr_body(view, run.context(m)),
                    branch,
                    approval.default,
                )
            except GitHubError as e:
                if e.status == 403:
                    fix = hint(self.lang, "hint_pull_403")
                else:
                    fix = hint(self.lang, "hint_pull_other", status=e.status, label=labels.build)
                raise Failure(str(e), fix) from e
            if report.evidence is not None and (report.evidence.items or report.evidence.pages):
                view = self._upload_evidence(view, git, pull.number, report.evidence, scratch, m)
            view = dataclasses.replace(view, pr_url=pull.url)
            body = render_build(view, run.context(m))
            run.finish(
                "pr_opened",
                body,
                [labels.build, labels.ready, labels.needs_human],
                [labels.built],
                metrics=m,
            )
            return 0
        outcome = _BUILD_OUTCOMES[report.status]
        run.finish(
            outcome,
            render_build(view, run.context(m)),
            [labels.build],
            [labels.needs_human],
            metrics=m,
        )
        return 0

    def _upload_evidence(
        self,
        view: BuildView,
        git: Git,
        number: int,
        evidence: EvidenceRun,
        scratch: Path,
        m: RunMetrics,
    ) -> BuildView:
        """Publish the evidence files and link them from the pull request; never fails the run."""
        run, env = self.run, self.run.env
        folder = f"pr-{number}"
        files = evidence_files(evidence)
        try:
            sha = publish(
                git,
                f"{env.server_url}/{env.repo}.git",
                env.token,
                folder,
                files,
                scratch / "evidence-branch",
            )
        except (GitError, EvidenceBranchError, GitHubError, OSError) as e:
            log(f"could not upload the evidence files: {e}")
            note = hint(self.lang, "evidence_upload_failed", why=str(e))
            return dataclasses.replace(view, evidence_note=note)
        links = {"folder": f"{env.server_url}/{env.repo}/tree/{EVIDENCE_BRANCH}/{folder}"}
        # At the published commit, so a later build's screenshots never replace these.
        links |= {
            name: f"{env.server_url}/{env.repo}/blob/{sha}/{folder}/{name}?raw=true"
            for name in files
            if name.endswith(".png")
        }
        linked = dataclasses.replace(view, evidence_links=links)
        try:
            run.tracker.update_pull(number, render_pr_body(linked, run.context(m)))
        except GitHubError as e:
            log(f"could not link the evidence files from the pull request: {e}")
            note = hint(self.lang, "evidence_link_failed", why=str(e))
            return dataclasses.replace(view, evidence_note=note)
        return linked


def _give_back(workspace: Path) -> None:
    """Hand what root created or rewrote in the checkout's .git back to the workspace owner."""
    try:
        owner = workspace.stat()
        moved = restore_owner(workspace / ".git", owner.st_uid, owner.st_gid)
    except OSError as e:
        log(f"could not give .git back to the workspace owner: {describe(e)}")
        return
    if moved:
        log(f"gave {len(moved)} .git entries back to uid {owner.st_uid}")
