"""The spec phase: read the thread, ask the planner, post questions or a spec."""

import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from specster.agent import AgentError, AgentOutcome, run_agent
from specster.approved import (
    ApprovedSpec,
    BuildRefused,
    comments_after,
    latest_spec_comment,
    load_spec,
)
from specster.config import Config, ModelConfig
from specster.event import Trigger
from specster.github import Comment, Issue
from specster.llm.base import ChatModel
from specster.llm.factory import ProviderConfigError
from specster.metrics import RunMetrics, spent
from specster.plan import max_parallel
from specster.pricing import cost_usd
from specster.prompts import context_block, revision_block, system_prompt
from specster.render import (
    render_budget,
    render_questions,
    render_spec,
)
from specster.repomap import RepoMap, build_repo_map
from specster.schemas import TASK_FILES_MAX, PlanTask, QuestionsResult
from specster.skills import Fetch, SkillBook
from specster.thread import Thread, build_thread
from specster.usecases.context import (
    LABEL_COLORS,
    NO_CHECKOUT,
    PROVIDER_HINT,
    UNEXPECTED_HINT,
    Failure,
    RunContext,
    describe,
    load_phase_skills,
    login_warnings_for,
    usage_fields,
)
from specster.workspace import Workspace

NO_REVISION = "previous spec has no valid plan marker: revision mode is off"


@dataclass(frozen=True)
class _Reading:
    """The issue as the planner will see it, and what the footer says about how it was read."""

    thread: Thread
    previous: ApprovedSpec | None
    login_warnings: list[str]
    revision_warnings: list[str]
    budget_warnings: list[str]
    thread_fields: dict[str, Any]
    known: float
    unknown: int


class SpecPhase:
    """`ai-spec`: read the thread, ask the planner, post its questions or its spec."""

    def __init__(
        self,
        run: RunContext,
        trigger: Trigger,
        make_model: Callable[[ModelConfig], ChatModel],
        fetch: Fetch,
        clock: Callable[[], datetime],
    ) -> None:
        self.run = run
        self.trigger = trigger
        self.make_model = make_model
        self.fetch = fetch
        self.clock = clock

    def execute(self) -> int:
        labels = self.run.cfg.labels
        self.run.tracker.ensure_labels(
            {
                labels.spec: LABEL_COLORS["spec"],
                labels.needs_human: LABEL_COLORS["needs_human"],
                labels.ready: LABEL_COLORS["ready"],
            }
        )
        reading = self._read()
        if self._stop_if_spent(reading):
            return 0
        self._publish(reading, *self._ask_planner(reading))
        return 0

    def _read(self) -> _Reading:
        run = self.run
        cfg, n, tracker = run.cfg, run.number, run.tracker
        issue = tracker.get_issue(n)
        snapshot = self._snapshot()
        comments = tracker.list_comments(n)
        own = tracker.own_login()
        login = cfg.identity.bot_login or own
        login_warnings = login_warnings_for(cfg.identity.bot_login, own)
        thread = build_thread(issue, comments, cfg.trust, snapshot, login=login)
        previous, revision_warnings = self._previous_spec(issue, comments, snapshot, login)
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
        return _Reading(
            thread,
            previous,
            login_warnings,
            revision_warnings,
            budget_warnings,
            thread_fields,
            known,
            unknown,
        )

    def _stop_if_spent(self, reading: _Reading) -> bool:
        run = self.run
        cap = run.cfg.budget.max_usd_per_issue
        if cap is None or reading.known < cap:
            return False
        warnings = reading.login_warnings + reading.budget_warnings
        m = run.metrics("budget_exhausted", 0.0, warnings=warnings, **reading.thread_fields)
        body = render_budget(reading.known, reading.unknown, cap, run.context(m, reading.thread))
        run.finish("budget_exhausted", body, [run.cfg.labels.spec], metrics=m)
        return True

    def _ask_planner(self, reading: _Reading) -> tuple[AgentOutcome, RunMetrics]:
        run, cfg = self.run, self.run.cfg
        ws, repo_map, skills, warnings = self._load_repo()
        user_text = reading.thread.text
        previous = reading.previous
        if previous is not None:
            user_text += "\n\n" + revision_block(previous, reading.thread.nonce)
        revision = previous is not None
        outcome = self._call_planner(user_text, ws, repo_map, skills, revision)
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
            warnings=warnings
            + reading.login_warnings
            + reading.revision_warnings
            + reading.budget_warnings
            + _task_size_warnings(outcome.tasks, cfg),
            revision=revision and not is_questions,
            **usage_fields(outcome.usage),
            **reading.thread_fields,
        )
        return outcome, m

    def _publish(self, reading: _Reading, outcome: AgentOutcome, m: RunMetrics) -> None:
        run, labels = self.run, self.run.cfg.labels
        ctx = run.context(m, reading.thread)
        try:
            if isinstance(outcome.result, QuestionsResult):
                body = render_questions(outcome.result, ctx)
                run.finish(
                    "questions", body, [labels.spec, labels.ready], [labels.needs_human], metrics=m
                )
            else:
                body = render_spec(outcome.result, outcome.tasks, outcome.plan_fixes, ctx)
                run.finish(
                    "spec", body, [labels.spec, labels.needs_human], [labels.ready], metrics=m
                )
        except Exception as e:
            # The model was paid for even if the reply or a label call fails, so bill it.
            traceback.print_exc()
            raise Failure(
                describe(e), UNEXPECTED_HINT, m.cost_usd, outcome.usage, outcome.turns
            ) from e

    def _snapshot(self) -> datetime:
        run = self.run
        labels, n = run.cfg.labels, run.number
        at: datetime | None = None
        if self.trigger.kind == "labeled" and run.cfg.trust.snapshot_at_label:
            at = run.tracker.label_applied_at(n, labels.spec)
        snapshot = at or self.clock()
        edited = run.tracker.body_edited_at(n)
        if edited is not None and edited > snapshot:
            raise Failure(
                "The issue body was edited after the label was added",
                f"Add the `{labels.spec}` label again after the last edit.",
            )
        return snapshot

    def _previous_spec(
        self, issue: Issue, comments: Sequence[Comment], snapshot: datetime, login: str | None
    ) -> tuple[ApprovedSpec | None, list[str]]:
        previous = latest_spec_comment(comments, login)
        if previous is None:
            return None, []
        trust = self.run.cfg.trust
        if not comments_after(comments, previous.created_at, issue, trust, snapshot, login=login):
            return None, []
        try:
            return load_spec(previous), []
        except BuildRefused:
            return None, [NO_REVISION]

    def _load_repo(self) -> tuple[Workspace, RepoMap, SkillBook, list[str]]:
        run = self.run
        cfg = run.cfg
        ws = Workspace(run.env.workspace, cfg.repo_map.exclude)
        warnings = [] if ws.files() else [NO_CHECKOUT]
        repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
        skills = load_phase_skills(run, self.fetch, "spec")
        return ws, repo_map, skills, warnings + skills.warnings

    def _call_planner(
        self, user_text: str, ws: Workspace, repo_map: RepoMap, skills: SkillBook, revision: bool
    ) -> AgentOutcome:
        cfg = self.run.cfg
        preview = cfg.build.preview is not None
        try:
            model = self.make_model(cfg.models.planner)
        except ProviderConfigError as e:
            raise Failure(describe(e), PROVIDER_HINT) from e
        try:
            return run_agent(
                model,
                system_prompt(cfg.persona, skills.on_demand, revision, preview),
                context_block(repo_map, skills.inline),
                user_text,
                ws,
                skills,
                cfg.budget.max_turns,
                cfg.persona.max_questions,
                revision=revision,
                preview=preview,
            )
        except AgentError as e:
            planner = cfg.models.planner
            cost = cost_usd(planner.provider, planner.model, e.usage, cfg.pricing)
            raise Failure(describe(e), PROVIDER_HINT, cost, e.usage, e.turns) from e
        except Exception as e:
            raise Failure(describe(e), PROVIDER_HINT, None) from e


def _task_size_warnings(tasks: Sequence[PlanTask], cfg: Config) -> list[str]:
    turns = cfg.build.max_turns_per_task
    return [
        f"task {t.id} touches {len(t.files)} files, and a worker has build.max_turns_per_task "
        f"({turns}) turns for all of them: split it before {cfg.labels.build} if it can be split"
        for t in tasks
        if len(t.files) > TASK_FILES_MAX
    ]
