import json
import sys
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from specster.agent import AgentError, AgentOutcome, run_agent
from specster.config import Config, ConfigError, LabelsConfig, ModelConfig, load_config
from specster.event import EventError, Trigger, parse_event, skip_reason
from specster.github import IssueTracker
from specster.llm.base import ChatModel, Usage
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics, spent
from specster.plan import max_parallel
from specster.pricing import cost_usd
from specster.prompts import context_block, system_prompt
from specster.render import (
    RenderContext,
    render_budget,
    render_error,
    render_questions,
    render_spec,
)
from specster.repomap import RepoMap, build_repo_map
from specster.schemas import QuestionsResult
from specster.skills import Fetch, SkillBook, SkillIntegrityError, load_skills
from specster.thread import Thread, build_thread
from specster.workspace import Workspace

LABEL_COLORS = {"spec": "5319e7", "needs_human": "fbca04", "ready": "0e8a16"}
_SECRET_INPUTS = {
    "INPUT_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
    "INPUT_OPENAI_API_KEY": "OPENAI_API_KEY",
    "INPUT_GEMINI_API_KEY": "GEMINI_API_KEY",
    "INPUT_AZURE_OPENAI_API_KEY": "AZURE_OPENAI_API_KEY",
}
NO_CHECKOUT = "repository not checked out: add actions/checkout before Specster"
PROVIDER_HINT = "Check the provider credentials and the model id in models.planner."
UNEXPECTED_HINT = "See the workflow log for details, then add the label again."
Outcome = Literal["questions", "spec", "error", "budget_exhausted"]
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
    )


class _Failure(Exception):
    def __init__(
        self,
        message: str,
        hint: str,
        cost: float | None = 0.0,
        usage: Usage | None = None,
        turns: int = 0,
    ) -> None:
        super().__init__(message)
        self.message, self.hint, self.cost = message, hint, cost
        self.usage, self.turns = usage or Usage(), turns


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

    def metrics(self, outcome: Outcome, cost: float | None, **fields: Any) -> RunMetrics:
        planner = self.cfg.models.planner
        return RunMetrics(
            run_id=self.env.run_id,
            outcome=outcome,
            provider=planner.provider,
            model=planner.model,
            cost_usd=cost,
            duration_s=round(self.timer() - self.started, 3),
            **fields,
        )

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
            ctx = self.context(
                self.metrics("error", failure.cost, turns=failure.turns, **_usage(failure.usage))
            )
            self.tracker.post_comment(self.number, render_error(failure.message, failure.hint, ctx))
        except Exception:
            traceback.print_exc()
        try:
            self.tracker.remove_label(self.number, self.cfg.labels.spec)
        except Exception:
            traceback.print_exc()
        _write_outcome(self.env, "error")
        return 1


def _usage(usage: Usage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
    }


def _read_trigger(env: Env) -> Trigger | None:
    try:
        payload = json.loads(env.event_path.read_text())
        return parse_event(env.event_name, payload, env.dispatch_issue)
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
        run = _Run(env, tracker, Config(), trigger.issue_number, started, timer)
        return run.fail(_Failure(str(e), f"Fix {env.config_path} and add the label again."))
    reason = skip_reason(trigger, cfg.labels)
    if reason:
        _log(f"skipped: {reason}")
        _write_outcome(env, "skipped")
        return 0
    run = _Run(env, tracker, cfg, trigger.issue_number, started, timer)
    try:
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


def _load_repo(run: _Run, fetch: Fetch) -> tuple[Workspace, RepoMap, SkillBook, list[str]]:
    cfg = run.cfg
    ws = Workspace(run.env.workspace, cfg.repo_map.exclude)
    warnings = [] if ws.files() else [NO_CHECKOUT]
    repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
    try:
        skills = load_skills(run.env.workspace, cfg.skills, "spec", fetch, run.env.skills_token)
    except SkillIntegrityError as e:
        raise _Failure(
            _describe(e), "Fix skills.sources: pin the current sha256 or use a path in the repo."
        ) from e
    except Exception as e:
        raise _Failure(
            _describe(e), "Check the skills.sources URLs and the skills_auth_token input."
        ) from e
    return ws, repo_map, skills, warnings + skills.warnings


def _call_agent(
    run: _Run,
    make_model: Callable[[ModelConfig], ChatModel],
    thread: Thread,
    ws: Workspace,
    repo_map: RepoMap,
    skills: SkillBook,
) -> AgentOutcome:
    cfg = run.cfg
    try:
        model = make_model(cfg.models.planner)
    except ProviderConfigError as e:
        raise _Failure(_describe(e), PROVIDER_HINT) from e
    try:
        return run_agent(
            model,
            system_prompt(cfg.persona, skills.on_demand),
            context_block(repo_map, skills.inline),
            thread.text,
            ws,
            skills,
            cfg.budget.max_turns,
            cfg.persona.max_questions,
        )
    except AgentError as e:
        planner = cfg.models.planner
        cost = cost_usd(planner.provider, planner.model, e.usage, cfg.pricing)
        raise _Failure(_describe(e), PROVIDER_HINT, cost, e.usage, e.turns) from e
    except Exception as e:
        raise _Failure(_describe(e), PROVIDER_HINT, None) from e


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
    thread = build_thread(issue, tracker.list_comments(n), cfg.trust, snapshot)
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
        m = run.metrics("budget_exhausted", 0.0, warnings=budget_warnings, **thread_fields)
        body = render_budget(known, unknown, cap, run.context(m, thread))
        run.finish("budget_exhausted", body, [labels.spec])
        return 0

    ws, repo_map, skills, warnings = _load_repo(run, fetch)
    outcome = _call_agent(run, make_model, thread, ws, repo_map, skills)
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
        warnings=warnings + budget_warnings,
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
