import dataclasses
import json
import shutil
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from specster.approved import (
    BuildRefused,
    approved_spec,
)
from specster.build import FINAL_SLOT, BuildReport, BuildSetup, run_build
from specster.closing import rewrite_references
from specster.config import Config, ConfigError, LabelsConfig, ModelConfig, load_config
from specster.event import EventError, Trigger, parse_event, phase_of, skip_reason
from specster.git import Author, Git, GitError, noreply_email
from specster.github import GitHubError, IssueTracker
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
from specster.repomap import build_repo_map
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
from specster.skills import Fetch, Skill, SkillBook
from specster.thread import previous_runs
from specster.usecases.context import (
    LABEL_COLORS,
    NO_CHECKOUT,
    UNEXPECTED_HINT,
    Env,
    Failure,
    Outcome,
    RunContext,
    describe,
    load_phase_skills,
    log,
    login_warnings_for,
    usage_fields,
    write_outcome,
)
from specster.usecases.spec_phase import (
    SpecPhase,
)
from specster.workspace import CONFIG_PATH, Workspace

# The composition root: `main` wires the real tracker, models and sandbox into a phase.
__all__ = ["Env", "env_from", "main"]

_SECRET_INPUTS = {
    "INPUT_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "INPUT_OPENAI_API_KEY": "OPENAI_API_KEY",
    "INPUT_GEMINI_API_KEY": "GEMINI_API_KEY",
    "INPUT_AZURE_OPENAI_API_KEY": "AZURE_OPENAI_API_KEY",
}


_BUILD_OUTCOMES: dict[str, Outcome] = {
    "approved": "pr_opened",
    "not_approved": "not_approved",
    "failed": "build_failed",
    "budget_exhausted": "budget_exhausted",
}


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
        config_path=environ.get("INPUT_CONFIG_PATH") or CONFIG_PATH,
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


def _read_trigger(env: Env) -> Trigger | None:
    try:
        payload = json.loads(env.event_path.read_text())
        return parse_event(env.event_name, payload, env.dispatch_issue, env.dispatch_phase)
    except (EventError, KeyError, TypeError, ValueError, OSError) as e:
        log(f"cannot read the event: {describe(e)}")
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
        write_outcome(env, "error")
        return 1
    try:
        cfg = load_config(env.workspace / env.config_path)
    except ConfigError as e:
        # A broken config cannot tell us the spec label, so only answer events that are
        # ours under the defaults; anything else (other labels, bots) stays silent.
        default_skip = skip_reason(trigger, LabelsConfig())
        if default_skip:
            log(f"{e} (not reported on the issue: {default_skip})")
            write_outcome(env, "error")
            return 1
        phase = phase_of(trigger, LabelsConfig())
        run = RunContext(env, tracker, Config(), trigger.issue_number, started, timer, phase)
        return run.fail(Failure(str(e), f"Fix {env.config_path} and add the label again."))
    reason = skip_reason(trigger, cfg.labels)
    if reason:
        log(f"skipped: {reason}")
        write_outcome(env, "skipped")
        return 0
    phase = phase_of(trigger, cfg.labels)
    run = RunContext(env, tracker, cfg, trigger.issue_number, started, timer, phase)
    try:
        if phase == "build":
            return _build_phase(run, trigger, make_model, fetch, identity)
        return SpecPhase(run, trigger, make_model, fetch, clock).execute()
    except Failure as failure:
        return run.fail(failure)
    except Exception as e:
        traceback.print_exc()
        return run.fail(Failure(describe(e), UNEXPECTED_HINT, None))


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
) -> tuple[Callable[[], ChatModel], Callable[[], ChatModel]]:
    models = run.cfg.models
    for key, cfg in (("models.worker", models.worker), ("models.reviewer", models.reviewer)):
        try:
            make_model(cfg)
        except ProviderConfigError as e:
            raise Failure(
                describe(e), hint(run.cfg.persona.language, "hint_role_model", key=key)
            ) from e
    return lambda: make_model(models.worker), lambda: make_model(models.reviewer)


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
        "test_runs": report.test_runs,
        "parallel_used": report.parallel_used,
        "review_rounds": report.review_rounds,
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


def _build_phase(
    run: RunContext,
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
    run.warnings = login_warnings_for(cfg.identity.bot_login, own)
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
            config_path=env.config_path,
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
        raise Failure(
            f"Branch {branch} already exists on the remote",
            hint(lang, "hint_branch_exists", label=labels.build),
        )
    try:
        require_root(identity(FINAL_SLOT))
    except SandboxError as e:
        raise Failure(str(e), hint(lang, "hint_root")) from e
    socket = docker_socket_problem(DOCKER_SOCKET)
    if socket is not None:
        raise Failure(socket, hint(lang, "hint_docker_socket", label=labels.build))
    make_worker, make_reviewer = _models(run, make_model)
    build_skills = load_phase_skills(run, fetch, "build")
    review_skills = load_phase_skills(run, fetch, "review")
    run.warnings += build_skills.warnings + [
        w for w in review_skills.warnings if w not in build_skills.warnings
    ]
    repo_map = build_repo_map(
        Workspace(env.workspace, cfg.repo_map.exclude), cfg.repo_map.max_tokens
    )
    scratch = scratch_dir()
    git: Git | None = None
    try:
        email = noreply_email(login, tracker.user_id(login) if login else None)
        git = Git(env.workspace, Author(cfg.persona.name, email), scratch / "git-home")
        try:
            base = git.head()
        except GitError as e:
            raise Failure(NO_CHECKOUT, hint(lang, "hint_checkout")) from e
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
            log(f"lock down: {line}")
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
        books = [build_skills, review_skills]
        run.build_facts = _skill_facts(books) | {"tasks_total": len(spec.tasks)}
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
                    env.config_path,
                )
            )
        except SandboxError as e:
            raise Failure(
                describe(e), hint(lang, "hint_sandbox", label=labels.build), needs_human=True
            ) from e
        truncations = [repo_map.truncation] if repo_map.truncation else []
        report.truncations[:0] = truncations
        run.build_facts = _report_facts(run, report, books)
        branch_url = None
        if report.commits > 0:
            try:
                git.push(f"{env.server_url}/{env.repo}.git", branch, env.token)
            except GitError as e:
                raise Failure(
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
        m = _build_metrics(run, ledger, report, books)
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
                raise Failure(str(e), fix) from e
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
                log(f"could not prune worktrees: {e}")
            _give_back(env.workspace)


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
