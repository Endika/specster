"""What every phase shares: the run's environment, its comment, its failure."""

import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from specster.approved import (
    BuildRefused,
    identity_warnings,
)
from specster.config import Config, ModelConfig, Phase
from specster.event import RunPhase
from specster.github import IssueTracker
from specster.ledger import Ledger
from specster.llm.base import Usage
from specster.metrics import RunMetrics
from specster.render import (
    RenderContext,
    render_error,
    render_refused,
)
from specster.skills import Fetch, SkillBook, SkillIntegrityError, load_skills
from specster.thread import Thread

LABEL_COLORS = {
    "spec": "5319e7",
    "needs_human": "fbca04",
    "ready": "0e8a16",
    "build": "1d76db",
    "built": "6f42c1",
}


NO_CHECKOUT = "repository not checked out: add actions/checkout before Specster"


PROVIDER_HINT = "Check the provider credentials and the model id in models.planner."


UNEXPECTED_HINT = "See the workflow log for details, then add the label again."


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


class Failure(Exception):
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


def describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def write_outcome(env: Env, outcome: StepOutcome) -> None:
    if env.output_path is not None:
        with env.output_path.open("a") as out:
            out.write(f"outcome={outcome}\n")


def log(message: str) -> None:
    print(f"specster: {message}", file=sys.stderr)


@dataclass
class RunContext:
    env: Env
    tracker: IssueTracker
    cfg: Config
    number: int
    started: float
    timer: Callable[[], float]
    phase: RunPhase = "spec"
    ledger: Ledger | None = None
    warnings: list[str] = field(default_factory=list)
    # What the build has shown so far, for the footer of a run that fails part way.
    build_facts: dict[str, Any] = field(default_factory=dict)

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
        log(f"refused: {r.message}")
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
        write_outcome(self.env, outcome)

    def fail(self, failure: Failure) -> int:
        log(failure.message)
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
                    **({"warnings": self.warnings} | self.build_facts),
                    **usage_fields(ledger.usage()),
                )
            else:
                m = self.metrics(
                    "error",
                    failure.cost,
                    role=self._build_role(),
                    turns=failure.turns,
                    warnings=self.warnings,
                    **usage_fields(failure.usage),
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
        write_outcome(self.env, "error")
        return 1


def usage_fields(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "output_tokens": usage.output_tokens,
    }


def load_phase_skills(run: RunContext, fetch: Fetch, phase: Phase) -> SkillBook:
    try:
        return load_skills(run.env.workspace, run.cfg.skills, phase, fetch, run.env.skills_token)
    except SkillIntegrityError as e:
        raise Failure(
            describe(e), "Fix skills.sources: pin the current sha256 or use a path in the repo."
        ) from e
    except Exception as e:
        raise Failure(
            describe(e), "Check the skills.sources URLs and the skills_auth_token input."
        ) from e


def login_warnings_for(configured: str | None, own: str | None) -> list[str]:
    login = configured or own
    warnings = identity_warnings(login) if login else [UNKNOWN_LOGIN]
    if configured and own and configured != own:
        warnings.append(
            f"identity.bot_login is {configured} but the token acts as {own}; "
            "Specster won't recognise its own comments"
        )
    return warnings
