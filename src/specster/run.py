import json
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from specster.config import Config, ConfigError, LabelsConfig, ModelConfig, load_config
from specster.event import EventError, Trigger, parse_event, phase_of, skip_reason
from specster.github import IssueTracker
from specster.llm.base import ChatModel
from specster.sandbox import (
    Identity,
    slot_identity,
)
from specster.skills import Fetch
from specster.usecases.build_phase import BuildPhase
from specster.usecases.context import (
    UNEXPECTED_HINT,
    Env,
    Failure,
    RunContext,
    describe,
    log,
    write_outcome,
)
from specster.usecases.spec_phase import (
    SpecPhase,
)
from specster.workspace import CONFIG_PATH

# The composition root: `main` wires the real tracker, models and sandbox into a phase.
__all__ = ["Env", "env_from", "main"]


_SECRET_INPUTS = {
    "INPUT_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "INPUT_OPENAI_API_KEY": "OPENAI_API_KEY",
    "INPUT_GEMINI_API_KEY": "GEMINI_API_KEY",
    "INPUT_AZURE_OPENAI_API_KEY": "AZURE_OPENAI_API_KEY",
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
            return BuildPhase(run, trigger, make_model, fetch, identity).execute()
        return SpecPhase(run, trigger, make_model, fetch, clock).execute()
    except Failure as failure:
        return run.fail(failure)
    except Exception as e:
        traceback.print_exc()
        return run.fail(Failure(describe(e), UNEXPECTED_HINT, None))
