import json
import time
import traceback
from collections.abc import Callable, Mapping
from contextvars import Token
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from opentelemetry import context, trace
from opentelemetry.context import Context
from opentelemetry.trace import StatusCode

from specster.browser import Installer, install
from specster.config import Config, ConfigError, LabelsConfig, ModelConfig, load_config
from specster.event import EventError, Trigger, parse_event, phase_of, skip_reason
from specster.github import PullTracker
from specster.llm.base import ChatModel
from specster.sandbox import (
    Identity,
    slot_identity,
)
from specster.skills import Fetch
from specster.telemetry import mark_error
from specster.telemetry_metrics import record_run
from specster.usecases.build_phase import BuildPhase
from specster.usecases.cleanup_phase import CleanupPhase
from specster.usecases.context import (
    UNEXPECTED_HINT,
    Env,
    Failure,
    RunContext,
    describe,
    log,
    write_outcome,
)
from specster.usecases.evidence_phase import EvidencePhase
from specster.usecases.fix_phase import FixPhase
from specster.usecases.pull_request import open_pull
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
        run_attempt=environ.get("GITHUB_RUN_ATTEMPT", "1"),
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
    tracker: PullTracker,
    make_model: Callable[[ModelConfig], ChatModel],
    fetch: Fetch,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    timer: Callable[[], float] = time.monotonic,
    identity: Callable[[int], Identity | None] = slot_identity,
    install_browser: Installer = install,
) -> int:
    started = timer()
    trigger = _read_trigger(env)
    if trigger is None:
        write_outcome(env, "error")
        return 1
    try:
        cfg = load_config(env.workspace / env.config_path)
    except ConfigError as e:
        if phase_of(trigger, LabelsConfig()) == "cleanup":
            log(f"{e} (not reported: the pull request is closed)")
            write_outcome(env, "error")
            return 1
        # A broken config cannot tell us the spec label, so only answer events that are
        # ours under the defaults; anything else (other labels, bots) stays silent.
        default_skip = skip_reason(trigger, LabelsConfig())
        if default_skip:
            log(f"{e} (not reported on the issue: {default_skip})")
            write_outcome(env, "error")
            return 1
        phase = phase_of(trigger, LabelsConfig())
        run = RunContext(env, tracker, Config(), trigger.issue_number, started, timer, phase)
        try:
            with _RootSpan(run):
                return run.fail(Failure(str(e), f"Fix {env.config_path} and add the label again."))
        finally:
            _record(run)
    reason = skip_reason(trigger, cfg.labels)
    if reason:
        log(f"skipped: {reason}")
        write_outcome(env, "skipped")
        return 0
    phase = phase_of(trigger, cfg.labels)
    run = RunContext(env, tracker, cfg, trigger.issue_number, started, timer, phase)
    try:
        with _RootSpan(run) as root:
            if phase == "cleanup":
                return CleanupPhase(run, trigger).execute()
            try:
                if phase == "build":
                    return BuildPhase(
                        run, trigger, make_model, fetch, identity, install_browser
                    ).execute()
                if phase in ("evidence", "fix"):
                    pull = open_pull(run, tracker)
                    if isinstance(pull, int):
                        return pull
                    kind = EvidencePhase if phase == "evidence" else FixPhase
                    return kind(
                        run, trigger, pull, tracker, make_model, fetch, identity, install_browser
                    ).execute()
                return SpecPhase(run, trigger, make_model, fetch, clock).execute()
            except Failure as failure:
                return run.fail(failure)
            except Exception as e:
                root.error(e)
                traceback.print_exc()
                return run.fail(Failure(describe(e), UNEXPECTED_HINT, None))
    finally:
        _record(run)


class _RootSpan:
    """The run's `specster.run` span; a tracing error is printed, never raised into the run."""

    def __init__(self, run: RunContext) -> None:
        self._run = run
        self._span: trace.Span | None = None
        self._token: Token[Context] | None = None

    def __enter__(self) -> "_RootSpan":
        try:
            attributes = {
                "specster.repo": self._run.env.repo,
                "specster.phase": self._run.phase,
                "specster.issue": self._run.number,
            }
            self._span = trace.get_tracer("specster").start_span(
                "specster.run", attributes=attributes
            )
            self._token = context.attach(trace.set_span_in_context(self._span))
        except Exception:
            traceback.print_exc()
        return self

    def error(self, e: BaseException) -> None:
        try:
            if self._span is not None:
                mark_error(self._span, e)
        except Exception:
            traceback.print_exc()

    def __exit__(
        self, kind: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        if e is not None:
            self.error(e)
        try:
            if self._span is not None and self._run.final is not None:
                self._span.set_attribute("specster.outcome", self._run.final[0])
                if self._run.final[0] == "error":
                    self._span.set_status(StatusCode.ERROR)
        except Exception:
            traceback.print_exc()
        try:
            if self._token is not None:
                context.detach(self._token)
            if self._span is not None:
                self._span.end()
        except Exception:
            traceback.print_exc()


def _record(run: RunContext) -> None:
    try:
        m = run.final[1] if run.final else None
        record_run(run.env.repo, m.phase if m is not None else run.phase, run.final)
    except Exception:
        traceback.print_exc()
