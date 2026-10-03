"""What every pull-request phase checks first: the checkout, and that the pull request is open,
ready and ours."""

import shutil
from collections.abc import Callable

import httpx

from specster.approved import BuildRefused
from specster.build import FINAL_SLOT
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.github import GitHubError, PullInfo, PullTracker
from specster.metrics import spent
from specster.render import hint, render_budget
from specster.sandbox import (
    DOCKER_SOCKET,
    Identity,
    SandboxError,
    docker_socket_problem,
    require_root,
    scratch_dir,
)
from specster.thread import previous_runs
from specster.usecases.context import (
    NO_CHECKOUT,
    Failure,
    RunContext,
    describe,
    login_warnings_for,
)


def _checkout_refusal(run: RunContext, pulls: PullTracker) -> BuildRefused | None:
    """Why the checkout is not the default branch's tip, where the config must come from."""
    lang, label = run.cfg.persona.language, run.trigger_label
    try:
        default = pulls.default_branch()
    except (GitHubError, httpx.HTTPError) as e:
        raise Failure(
            f"could not read the default branch: {describe(e)}",
            hint(lang, "hint_pull_read", label=label),
        ) from e
    home = scratch_dir()
    try:
        git = Git(run.env.workspace, Author(run.cfg.persona.name, BOT_EMAIL), home / "git-home")
        try:
            head = git.head()
        except GitError as e:
            raise Failure(NO_CHECKOUT, hint(lang, "hint_checkout")) from e
        ref = f"refs/remotes/origin/{default}^{{commit}}"
        try:
            tip = git.run("rev-parse", "--verify", "--quiet", "--end-of-options", ref).strip()
        except GitError:
            # A pull request's own checkout fetches only its merge ref: fail closed.
            tip = None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if tip == head:
        return None
    where = f"the tip of {default} ({tip[:12]})" if tip else f"{default}, which was not fetched"
    return BuildRefused(
        f"The checkout is at {head[:12]}, not at {where}",
        hint(lang, "hint_pull_checkout", label=label),
    )


def open_pull(run: RunContext, pulls: PullTracker) -> PullInfo | int:
    """The labeled pull request, or the exit code of its refusal."""
    lang, label = run.cfg.persona.language, run.trigger_label
    # The workflow checks out the default branch; a workflow that checks out the pull request
    # would have handed Specster the pull request's own config.
    refusal = _checkout_refusal(run, pulls)
    if refusal is not None:
        return run.refuse(refusal)
    try:
        pull = pulls.get_pull(run.number)
    except (GitHubError, httpx.HTTPError) as e:
        raise Failure(
            f"could not read pull request #{run.number}: {describe(e)}",
            hint(lang, "hint_pull_read", label=label),
        ) from e
    # A fork's head is code its author controls; the workflow's `if` checks this too.
    if not pull.head_repo or pull.head_repo != pull.base_repo:
        origin = pull.head_repo or "a deleted fork"
        message = f"This pull request comes from {origin}, not from {pull.base_repo}"
        return run.refuse(BuildRefused(message, hint(lang, "hint_pull_fork")))
    if pull.state != "open":
        message = f"Pull request #{pull.number} is {pull.state}"
        return run.refuse(BuildRefused(message, hint(lang, "hint_pull_closed", label=label)))
    if pull.draft:
        message = f"Pull request #{pull.number} is a draft"
        return run.refuse(BuildRefused(message, hint(lang, "hint_pull_draft", label=label)))
    return pull


def known_spend(run: RunContext) -> float | None:
    """What earlier runs on this pull request spent; None once a spent budget is reported."""
    cfg, n = run.cfg, run.number
    comments = run.tracker.list_comments(n)
    own = run.tracker.own_login()
    run.warnings = login_warnings_for(cfg.identity.bot_login, own)
    known, unknown = spent(previous_runs(comments, cfg.identity.bot_login or own))
    if unknown:
        run.warnings.append(
            f"{unknown} previous runs have unknown cost and are not counted in the budget"
        )
    cap = cfg.budget.max_usd_per_issue
    if cap is None or known < cap:
        return known
    m = run.metrics("budget_exhausted", 0.0, warnings=run.warnings)
    body = render_budget(known, unknown, cap, run.context(m), pull=True)
    run.finish("budget_exhausted", body, [run.trigger_label], metrics=m)
    return None


def check_host(run: RunContext, identity: Callable[[int], Identity | None]) -> None:
    """Root to drop privileges into the sandboxes, and no Docker socket they could reach."""
    lang, label = run.cfg.persona.language, run.trigger_label
    try:
        require_root(identity(FINAL_SLOT))
    except SandboxError as e:
        raise Failure(str(e), hint(lang, "hint_root")) from e
    socket = docker_socket_problem(DOCKER_SOCKET)
    if socket is not None:
        raise Failure(socket, hint(lang, "hint_docker_socket", label=label))
