import dataclasses
import json
import shutil
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from specster.agent import AgentError, AgentOutcome, run_agent
from specster.approved import (
    ApprovedSpec,
    BuildRefused,
    approved_spec,
    comments_after,
    identity_warnings,
    latest_spec_comment,
    load_spec,
)
from specster.build import FINAL_SLOT, BuildReport, BuildSetup, run_build
from specster.closing import rewrite_references
from specster.config import Config, ConfigError, LabelsConfig, ModelConfig, Phase, load_config
from specster.event import EventError, RunPhase, Trigger, parse_event, phase_of, skip_reason
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.github import Comment, GitHubError, Issue, IssueTracker
from specster.ledger import Ledger
from specster.llm.base import ChatModel, Usage
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics, spent
from specster.plan import max_parallel
from specster.pricing import cost_usd
from specster.prompts import context_block, revision_block, system_prompt
from specster.render import (
    BuildView,
    RenderContext,
    hint,
    render_budget,
    render_build,
    render_error,
    render_pr_body,
    render_questions,
    render_refused,
    render_spec,
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
    slot_identity,
)
from specster.schemas import QuestionsResult
from specster.skills import Fetch, Skill, SkillBook, SkillIntegrityError, load_skills
from specster.thread import Thread, build_thread, previous_runs
from specster.workspace import Workspace

LABEL_COLORS = {
    "spec": "5319e7",
    "needs_human": "fbca04",
    "ready": "0e8a16",
    "build": "1d76db",
    "built": "6f42c1",
}
_SECRET_INPUTS = {
    "INPUT_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "INPUT_OPENAI_API_KEY": "OPENAI_API_KEY",
    "INPUT_GEMINI_API_KEY": "GEMINI_API_KEY",
    "INPUT_AZURE_OPENAI_API_KEY": "AZURE_OPENAI_API_KEY",
}
NO_CHECKOUT = "repository not checked out: add actions/checkout before Specster"
PROVIDER_HINT = "Check the provider credentials and the model id in models.planner."
UNEXPECTED_HINT = "See the workflow log for details, then add the label again."
NO_REVISION = "previous spec has no valid plan marker: revision mode is off"
UNKNOWN_LOGIN = (
    "Specster's bot login is unknown: any bot comment with a Specster marker counts as "
    "Specster's; set identity.bot_login"
)
Outcome = Literal[
    "questions",
    "spec",
    "error",
    "budget_exhausted",
    "refused",
    "pr_opened",
    "not_approved",
    "build_failed",
]
_BUILD_OUTCOMES: dict[str, Outcome] = {
    "approved": "pr_opened",
    "not_approved": "not_approved",
    "failed": "build_failed",
    "budget_exhausted": "budget_exhausted",
}
StepOutcome = Outcome | Literal["skipped"]


@dataclass(frozen=True)
class Env:
    workspace: Path
    event_name: str
    event_path: Path
    repo: str
    run_id: str
    token: str
    config_path: str
    dispatch_issue: str | None
    api_url: str
    graphql_url: str
    skills_token: str | None
    secrets: Mapping[str, str]
    output_path: Path | None
    process_env: Mapping[str, str] = field(default_factory=dict)
    server_url: str = "https://github.com"
    ref: str = ""
    dispatch_phase: str | None = None
    sha: str = ""


def _google_credentials(environ: Mapping[str, str]) -> dict[str, str]:
    # In a Docker action GOOGLE_APPLICATION_CREDENTIALS still names the runner's host path;
    # the file google-github-actions/auth wrote is mounted under GITHUB_WORKSPACE instead.
    host = environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    workspace = environ.get("GITHUB_WORKSPACE")
    if not host or not workspace or Path(host).exists():
        return {}
    local = Path(workspace) / Path(host).name
    return {"GOOGLE_APPLICATION_CREDENTIALS": str(local)} if local.is_file() else {}


def env_from(environ: Mapping[str, str]) -> Env:
    secrets = dict(environ)
    for src, dst in _SECRET_INPUTS.items():
        if environ.get(src):
            secrets[dst] = environ[src]
    process_env = _google_credentials(environ)
    secrets.update(process_env)
    out = environ.get("GITHUB_OUTPUT")
    return Env(
        workspace=Path(environ.get("GITHUB_WORKSPACE", ".")),
        event_name=environ.get("GITHUB_EVENT_NAME", ""),
        event_path=Path(environ.get("GITHUB_EVENT_PATH", "event.json")),
        repo=environ.get("GITHUB_REPOSITORY", ""),
        run_id=environ.get("GITHUB_RUN_ID", "local"),
        token=environ.get("INPUT_GITHUB_TOKEN") or environ.get("GITHUB_TOKEN", ""),
        config_path=environ.get("INPUT_CONFIG_PATH") or ".github/specster/config.yml",
        dispatch_issue=environ.get("INPUT_ISSUE_NUMBER") or None,
        api_url=environ.get("GITHUB_API_URL", "https://api.github.com"),
        graphql_url=environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
        skills_token=environ.get("INPUT_SKILLS_AUTH_TOKEN") or None,
        secrets=secrets,
        output_path=Path(out) if out else None,
        process_env=process_env,
        server_url=environ.get("GITHUB_SERVER_URL") or "https://github.com",
        ref=environ.get("GITHUB_REF", ""),
        dispatch_phase=environ.get("INPUT_PHASE") or None,
        sha=environ.get("GITHUB_SHA", ""),
    )


class _Failure(Exception):
    def __init__(
        self,
        message: str,
        hint: str,
        cost: float | None = 0.0,
        usage: Usage | None = None,
        turns: int = 0,
        needs_human: bool = False,
    ) -> None:
        super().__init__(message)
        self.message, self.hint, self.cost = message, hint, cost
        self.usage, self.turns = usage or Usage(), turns
        self.needs_human = needs_human


def _describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _write_outcome(env: Env, outcome: StepOutcome) -> None:
    if env.output_path is not None:
        with env.output_path.open("a") as out:
            out.write(f"outcome={outcome}\n")


def _log(message: str) -> None:
    print(f"specster: {message}", file=sys.stderr)


@dataclass
class _Run:
    env: Env
    tracker: IssueTracker
    cfg: Config
    number: int
    started: float
    timer: Callable[[], float]
    phase: RunPhase = "spec"
    ledger: Ledger | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def trigger_label(self) -> str:
        return self.cfg.labels.build if self.phase == "build" else self.cfg.labels.spec

    def metrics(
        self,
        outcome: Outcome,
        cost: float | None,
        role: ModelConfig | None = None,
        **fields: Any,
    ) -> RunMetrics:
        model = role or self.cfg.models.planner
        fields.setdefault("phase", self.phase)
        return RunMetrics(
            run_id=self.env.run_id,
            outcome=outcome,
            provider=model.provider,
            model=model.model,
            cost_usd=cost,
            duration_s=round(self.timer() - self.started, 3),
            **fields,
        )

    def _build_role(self) -> ModelConfig | None:
        return self.cfg.models.worker if self.phase == "build" else None

    def refuse(self, r: BuildRefused) -> int:
        _log(f"refused: {r.message}")
        base = f"{self.env.server_url}/{self.env.repo}/issues/{self.number}"
        links = [(c.author, f"{base}#issuecomment-{c.id}") for c in r.comments]
        m = self.metrics("refused", 0.0, role=self._build_role(), warnings=self.warnings)
        body = render_refused(r.message, r.hint, self.context(m), links)
        self.finish("refused", body, [self.trigger_label])
        return 1

    def context(self, metrics: RunMetrics, thread: Thread | None = None) -> RenderContext:
        hidden = thread.hidden if thread else ()
        untrusted = thread.untrusted if thread else ()
        labels = self.cfg.labels
        return RenderContext(
            self.cfg.persona, metrics, hidden, untrusted, labels.spec, labels.build
        )

    def finish(
        self, outcome: Outcome, body: str, remove: Sequence[str], add: Sequence[str] = ()
    ) -> None:
        self.tracker.post_comment(self.number, body)
        for label in remove:
            self.tracker.remove_label(self.number, label)
        if add:
            self.tracker.add_labels(self.number, list(add))
        _write_outcome(self.env, outcome)

    def fail(self, failure: _Failure) -> int:
        _log(failure.message)
        try:
            if self.ledger is not None:
                ledger = self.ledger
                m = self.metrics(
                    "error",
                    ledger.cost(),
                    role=self._build_role(),
                    phase="build",
                    roles=ledger.roles(),
                    turns=ledger.turns(),
                    warnings=self.warnings,
                    **_usage(ledger.usage()),
                )
            else:
                m = self.metrics(
                    "error",
                    failure.cost,
                    role=self._build_role(),
                    turns=failure.turns,
                    warnings=self.warnings,
                    **_usage(failure.usage),
                )
            body = render_error(failure.message, failure.hint, self.context(m))
            self.tracker.post_comment(self.number, body)
        except Exception:
            traceback.print_exc()
        try:
            self.tracker.remove_label(self.number, self.trigger_label)
        except Exception:
            traceback.print_exc()
        if failure.needs_human:
            try:
                self.tracker.add_labels(self.number, [self.cfg.labels.needs_human])
            except Exception:
                traceback.print_exc()
        _write_outcome(self.env, "error")
        return 1


def _usage(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
    }


def _read_trigger(env: Env) -> Trigger | None:
    try:
        payload = json.loads(env.event_path.read_text())
        return parse_event(env.event_name, payload, env.dispatch_issue, env.dispatch_phase)
    except (EventError, KeyError, TypeError, ValueError, OSError) as e:
        _log(f"cannot read the event: {_describe(e)}")
        return None


def main(
    env: Env,
    tracker: IssueTracker,
    make_model: Callable[[ModelConfig], ChatModel],
    fetch: Fetch,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: Callable[[], float] = time.monotonic,
    identity: Callable[[int], Identity | None] = slot_identity,
) -> int:
    started = timer()
    trigger = _read_trigger(env)
    if trigger is None:
        _write_outcome(env, "error")
        return 1
    try:
        cfg = load_config(env.workspace / env.config_path)
    except ConfigError as e:
        # A broken config cannot tell us the spec label, so only answer events that are
        # ours under the defaults; anything else (other labels, bots) stays silent.
        default_skip = skip_reason(trigger, LabelsConfig())
        if default_skip:
            _log(f"{e} (not reported on the issue: {default_skip})")
            _write_outcome(env, "error")
            return 1
        phase = phase_of(trigger, LabelsConfig())
        run = _Run(env, tracker, Config(), trigger.issue_number, started, timer, phase)
        return run.fail(_Failure(str(e), f"Fix {env.config_path} and add the label again."))
    reason = skip_reason(trigger, cfg.labels)
    if reason:
        _log(f"skipped: {reason}")
        _write_outcome(env, "skipped")
        return 0
    phase = phase_of(trigger, cfg.labels)
    run = _Run(env, tracker, cfg, trigger.issue_number, started, timer, phase)
    try:
        if phase == "build":
            return _build_phase(run, trigger, make_model, fetch, identity)
        return _spec_phase(run, trigger, make_model, fetch, clock)
    except _Failure as failure:
        return run.fail(failure)
    except Exception as e:
        traceback.print_exc()
        return run.fail(_Failure(_describe(e), UNEXPECTED_HINT, None))


def _snapshot(run: _Run, trigger: Trigger, clock: Callable[[], datetime]) -> datetime:
    labels, n = run.cfg.labels, run.number
    at: datetime | None = None
    if trigger.kind == "labeled" and run.cfg.trust.snapshot_at_label:
        at = run.tracker.label_applied_at(n, labels.spec)
    snapshot = at or clock()
    edited = run.tracker.body_edited_at(n)
    if edited is not None and edited > snapshot:
        raise _Failure(
            "The issue body was edited after the label was added",
            f"Add the `{labels.spec}` label again after the last edit.",
        )
    return snapshot


def _load_skills(run: _Run, fetch: Fetch, phase: Phase) -> SkillBook:
    try:
        return load_skills(run.env.workspace, run.cfg.skills, phase, fetch, run.env.skills_token)
    except SkillIntegrityError as e:
        raise _Failure(
            _describe(e), "Fix skills.sources: pin the current sha256 or use a path in the repo."
        ) from e
    except Exception as e:
        raise _Failure(
            _describe(e), "Check the skills.sources URLs and the skills_auth_token input."
        ) from e


def _load_repo(
    run: _Run, fetch: Fetch, phase: Phase = "spec"
) -> tuple[Workspace, RepoMap, SkillBook, list[str]]:
    cfg = run.cfg
    ws = Workspace(run.env.workspace, cfg.repo_map.exclude)
    warnings = [] if ws.files() else [NO_CHECKOUT]
    repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
    skills = _load_skills(run, fetch, phase)
    return ws, repo_map, skills, warnings + skills.warnings


def _call_agent(
    run: _Run,
    make_model: Callable[[ModelConfig], ChatModel],
    user_text: str,
    ws: Workspace,
    repo_map: RepoMap,
    skills: SkillBook,
    revision: bool,
) -> AgentOutcome:
    cfg = run.cfg
    try:
        model = make_model(cfg.models.planner)
    except ProviderConfigError as e:
        raise _Failure(_describe(e), PROVIDER_HINT) from e
    try:
        return run_agent(
            model,
            system_prompt(cfg.persona, skills.on_demand, revision),
            context_block(repo_map, skills.inline),
            user_text,
            ws,
            skills,
            cfg.budget.max_turns,
            cfg.persona.max_questions,
            revision=revision,
        )
    except AgentError as e:
        planner = cfg.models.planner
        cost = cost_usd(planner.provider, planner.model, e.usage, cfg.pricing)
        raise _Failure(_describe(e), PROVIDER_HINT, cost, e.usage, e.turns) from e
    except Exception as e:
        raise _Failure(_describe(e), PROVIDER_HINT, None) from e


def _previous_spec(
    run: _Run, issue: Issue, comments: Sequence[Comment], snapshot: datetime, login: str | None
) -> tuple[ApprovedSpec | None, list[str]]:
    previous = latest_spec_comment(comments, login)
    if previous is None:
        return None, []
    trust = run.cfg.trust
    if not comments_after(comments, previous.created_at, issue, trust, snapshot, login=login):
        return None, []
    try:
        return load_spec(previous), []
    except BuildRefused:
        return None, [NO_REVISION]


def _spec_phase(
    run: _Run,
    trigger: Trigger,
    make_model: Callable[[ModelConfig], ChatModel],
    fetch: Fetch,
    clock: Callable[[], datetime],
) -> int:
    cfg, labels, n, tracker = run.cfg, run.cfg.labels, run.number, run.tracker
    tracker.ensure_labels(
        {
            labels.spec: LABEL_COLORS["spec"],
            labels.needs_human: LABEL_COLORS["needs_human"],
            labels.ready: LABEL_COLORS["ready"],
        }
    )
    issue = tracker.get_issue(n)
    snapshot = _snapshot(run, trigger, clock)
    comments = tracker.list_comments(n)
    own = tracker.own_login()
    login = cfg.identity.bot_login or own
    login_warnings = _login_warnings(cfg.identity.bot_login, own)
    thread = build_thread(issue, comments, cfg.trust, snapshot, login=login)
    previous, revision_warnings = _previous_spec(run, issue, comments, snapshot, login)
    thread_fields: dict[str, Any] = {
        "comments_included": thread.included,
        "comments_untrusted": len(thread.untrusted),
        "comments_after_label": thread.after_label,
        "comments_edited_after_label": thread.edited_after_label,
        "hidden_removed": len(thread.hidden),
    }
    known, unknown = spent(thread.previous_runs)
    budget_warnings = (
        [f"{unknown} previous runs have unknown cost and are not counted in the budget"]
        if unknown
        else []
    )
    cap = cfg.budget.max_usd_per_issue
    if cap is not None and known >= cap:
        spend_warnings = login_warnings + budget_warnings
        m = run.metrics("budget_exhausted", 0.0, warnings=spend_warnings, **thread_fields)
        body = render_budget(known, unknown, cap, run.context(m, thread))
        run.finish("budget_exhausted", body, [labels.spec])
        return 0

    ws, repo_map, skills, warnings = _load_repo(run, fetch)
    user_text = thread.text
    if previous is not None:
        user_text += "\n\n" + revision_block(previous, thread.nonce)
    revision = previous is not None
    outcome = _call_agent(run, make_model, user_text, ws, repo_map, skills, revision)
    planner = cfg.models.planner
    is_questions = isinstance(outcome.result, QuestionsResult)
    m = run.metrics(
        "questions" if is_questions else "spec",
        cost_usd(planner.provider, planner.model, outcome.usage, cfg.pricing),
        turns=outcome.turns,
        files_read=sorted(ws.files_read),
        skills_available=[s.name for s in skills.inline + skills.on_demand],
        skills_read=sorted(skills.read),
        skills_inlined=[s.name for s in skills.inline],
        plan_max_parallel=None if is_questions else max_parallel(outcome.tasks),
        truncations=([repo_map.truncation] if repo_map.truncation else []) + ws.truncations,
        warnings=warnings + login_warnings + revision_warnings + budget_warnings,
        revision=revision and not is_questions,
        **_usage(outcome.usage),
        **thread_fields,
    )
    ctx = run.context(m, thread)
    try:
        if isinstance(outcome.result, QuestionsResult):
            body = render_questions(outcome.result, ctx)
            run.finish("questions", body, [labels.spec, labels.ready], [labels.needs_human])
        else:
            body = render_spec(outcome.result, outcome.tasks, outcome.plan_fixes, ctx)
            run.finish("spec", body, [labels.spec, labels.needs_human], [labels.ready])
    except Exception as e:
        # The model was paid for even if the reply or a label call fails, so bill it.
        traceback.print_exc()
        raise _Failure(
            _describe(e), UNEXPECTED_HINT, m.cost_usd, outcome.usage, outcome.turns
        ) from e
    return 0


def _login_warnings(configured: str | None, own: str | None) -> list[str]:
    login = configured or own
    warnings = identity_warnings(login) if login else [UNKNOWN_LOGIN]
    if configured and own and configured != own:
        warnings.append(
            f"identity.bot_login is {configured} but the token acts as {own}; "
            "Specster won't recognise its own comments"
        )
    return warnings


def _low_budget(run: _Run, known: float) -> list[str]:
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
    run: _Run, make_model: Callable[[ModelConfig], ChatModel]
) -> tuple[Callable[[], ChatModel], Callable[[], ChatModel]]:
    models = run.cfg.models
    for key, cfg in (("models.worker", models.worker), ("models.reviewer", models.reviewer)):
        try:
            make_model(cfg)
        except ProviderConfigError as e:
            raise _Failure(
                _describe(e), hint(run.cfg.persona.language, "hint_role_model", key=key)
            ) from e
    return lambda: make_model(models.worker), lambda: make_model(models.reviewer)


def _build_metrics(
    run: _Run, ledger: Ledger, report: BuildReport, books: Sequence[SkillBook], **fields: Any
) -> RunMetrics:
    def names(skills: Sequence[Skill]) -> list[str]:
        return list(dict.fromkeys(s.name for s in skills))

    return run.metrics(
        _BUILD_OUTCOMES[report.status],
        ledger.cost(),
        role=run.cfg.models.worker,
        phase="build",
        roles=ledger.roles(),
        tasks_total=len(report.tasks),
        tasks_done=sum(1 for r in report.tasks if r.status == "done"),
        test_runs=report.test_runs,
        parallel_used=report.parallel_used,
        review_rounds=report.review_rounds,
        turns=ledger.turns(),
        truncations=report.truncations,
        warnings=run.warnings + report.warnings,
        skills_available=names([s for b in books for s in b.inline + b.on_demand]),
        skills_inlined=names([s for b in books for s in b.inline]),
        skills_read=sorted({n for b in books for n in b.read}),
        **_usage(ledger.usage()),
        **fields,
    )


def _default_tip(git: Git, default: str, event_sha: str) -> str | None:
    ref = f"refs/remotes/origin/{default}^{{commit}}"
    try:
        return git.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref).strip()
    except GitError:
        return event_sha or None


def _build_phase(
    run: _Run,
    trigger: Trigger,
    make_model: Callable[[ModelConfig], ChatModel],
    fetch: Fetch,
    identity: Callable[[int], Identity | None],
) -> int:
    cfg, labels, n, tracker, env = run.cfg, run.cfg.labels, run.number, run.tracker, run.env
    lang = cfg.persona.language
    tracker.ensure_labels(
        {
            labels.build: LABEL_COLORS["build"],
            labels.built: LABEL_COLORS["built"],
            labels.needs_human: LABEL_COLORS["needs_human"],
        }
    )
    issue = tracker.get_issue(n)
    comments = tracker.list_comments(n)
    own = tracker.own_login()
    login = cfg.identity.bot_login or own
    run.warnings = _login_warnings(cfg.identity.bot_login, own)
    label_at = tracker.label_applied_at(n, labels.build) if trigger.kind == "labeled" else None
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
        )
    except BuildRefused as r:
        return run.refuse(r)
    default = tracker.default_branch()
    if env.ref and env.ref != f"refs/heads/{default}":
        return run.refuse(
            BuildRefused(
                f"The workflow ran on {env.ref}, not on the default branch {default}",
                hint(lang, "hint_default_branch"),
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
            "budget_exhausted", render_budget(known, unknown, cap, run.context(m)), [labels.build]
        )
        return 0
    run.warnings += _low_budget(run, known)
    branch = f"specster/issue-{n}"
    if tracker.branch_exists(branch):
        raise _Failure(
            f"Branch {branch} already exists on the remote",
            hint(lang, "hint_branch_exists", label=labels.build),
        )
    try:
        require_root(identity(FINAL_SLOT))
    except SandboxError as e:
        raise _Failure(str(e), hint(lang, "hint_root")) from e
    socket = docker_socket_problem(DOCKER_SOCKET)
    if socket is not None:
        raise _Failure(socket, hint(lang, "hint_docker_socket", label=labels.build))
    make_worker, make_reviewer = _models(run, make_model)
    build_skills = _load_skills(run, fetch, "build")
    review_skills = _load_skills(run, fetch, "review")
    run.warnings += build_skills.warnings + [
        w for w in review_skills.warnings if w not in build_skills.warnings
    ]
    repo_map = build_repo_map(
        Workspace(env.workspace, cfg.repo_map.exclude), cfg.repo_map.max_tokens
    )
    scratch = scratch_dir()
    git: Git | None = None
    try:
        git = Git(env.workspace, Author(cfg.persona.name, BOT_EMAIL), scratch / "git-home")
        try:
            base = git.head()
        except GitError as e:
            raise _Failure(NO_CHECKOUT, hint(lang, "hint_checkout")) from e
        tip = _default_tip(git, default, env.sha)
        if tip is not None and tip != base:
            return run.refuse(
                BuildRefused(
                    f"The checkout is at {base[:12]}, not at the tip of {default} ({tip[:12]}) "
                    "that the event saw",
                    hint(lang, "hint_head", label=labels.build),
                )
            )
        for line in lock_down(env.workspace, git, env.secrets):
            _log(f"lock down: {line}")
        locked_git = git
        build = cfg.build
        deadline = run.started + build.max_minutes * 60

        def time_left() -> float:
            return deadline - run.timer()

        def make_sandbox(slot: int) -> Sandbox:
            sandbox = Sandbox(
                identity(slot),
                build.test_timeout_s,
                build.test_output_max_kb * 1024,
                build.test_env,
                max_file_bytes=build.test_output_max_file_mb * 1024 * 1024,
                time_left=time_left,
            )
            sandbox.lock_down(env.workspace, locked_git, env.secrets)
            return sandbox

        ledger = run.ledger = Ledger(cfg.pricing)
        try:
            report = run_build(
                BuildSetup(
                    cfg,
                    spec,
                    git,
                    make_sandbox,
                    ledger,
                    make_worker,
                    make_reviewer,
                    build_skills,
                    review_skills,
                    repo_map,
                    branch,
                    base,
                    scratch,
                    known,
                    cfg.persona,
                    time_left,
                )
            )
        except SandboxError as e:
            raise _Failure(
                _describe(e), hint(lang, "hint_sandbox", label=labels.build), needs_human=True
            ) from e
        truncations = [repo_map.truncation] if repo_map.truncation else []
        report.truncations[:0] = truncations
        branch_url = None
        if report.commits > 0:
            try:
                git.push(f"{env.server_url}/{env.repo}.git", branch, env.token)
            except GitError as e:
                raise _Failure(
                    f"the branch could not be pushed: {e}",
                    hint(lang, "hint_push", label=labels.build),
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
            [(c.author, f"{issue_url}#issuecomment-{c.id}") for c in after],
            n,
            build.close_issue,
        )
        m = _build_metrics(run, ledger, report, [build_skills, review_skills])
        if report.status == "approved":
            try:
                pull = tracker.create_pull(
                    spec_title(spec.text) or rewrite_references(" ".join(issue.title.split())),
                    render_pr_body(view, run.context(m)),
                    branch,
                    default,
                )
            except GitHubError as e:
                if e.status == 403:
                    fix = hint(lang, "hint_pull_403")
                else:
                    fix = hint(lang, "hint_pull_other", status=e.status, label=labels.build)
                raise _Failure(str(e), fix) from e
            view = dataclasses.replace(view, pr_url=pull.url)
            body = render_build(view, run.context(m))
            run.finish(
                "pr_opened",
                body,
                [labels.build, labels.ready, labels.needs_human],
                [labels.built],
            )
            return 0
        outcome = _BUILD_OUTCOMES[report.status]
        run.finish(
            outcome, render_build(view, run.context(m)), [labels.build], [labels.needs_human]
        )
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if git is not None:
            try:
                git.run("worktree", "prune")
            except GitError as e:
                _log(f"could not prune worktrees: {e}")
            _give_back(env.workspace)


def _give_back(workspace: Path) -> None:
    """Hand what root created or rewrote in the checkout's .git back to the workspace owner."""
    try:
        owner = workspace.stat()
        moved = restore_owner(workspace / ".git", owner.st_uid, owner.st_gid)
    except OSError as e:
        _log(f"could not give .git back to the workspace owner: {_describe(e)}")
        return
    if moved:
        _log(f"gave {len(moved)} .git entries back to uid {owner.st_uid}")
