from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from specster.agent import (
    Handler,
    SubmissionError,
    Submit,
    read_handlers,
    read_tool_specs,
    run_loop,
    skill_handler,
    skill_spec,
)
from specster.llm.base import ChatModel, ToolSpec, Usage
from specster.schemas import SUBMIT_REVIEW, Finding, ReviewResult, json_schema
from specster.skills import SkillBook
from specster.workspace import Workspace

NUDGE = "Call submit_review now."
BLOCKING = frozenset({"critical", "important"})


def blocking(result: ReviewResult) -> list[Finding]:
    return [f for f in result.findings if f.severity in BLOCKING]


@dataclass(frozen=True)
class ReviewOutcome:
    result: ReviewResult
    usage: Usage
    turns: int


def run_review(
    model: ChatModel,
    system: str,
    context: str,
    user: str,
    ws: Workspace,
    skills: SkillBook,
    task_ids: Collection[str],
    max_turns: int,
) -> ReviewOutcome:
    ids = set(task_ids)

    def submit(args: dict[str, Any]) -> ReviewResult:
        result = ReviewResult.model_validate(args)
        unknown = sorted({f.task_id for f in result.findings if f.task_id not in ids})
        if unknown:
            allowed = ", ".join(sorted(ids))
            raise SubmissionError(f"unknown task_id {', '.join(unknown)}; use one of: {allowed}")
        blocked = blocking(result)
        if result.verdict == "approve" and blocked:
            raise SubmissionError("approve cannot carry critical or important findings")
        if result.verdict == "changes" and not blocked:
            raise SubmissionError(
                "changes needs at least one critical or important finding; minor ones go "
                "with approve"
            )
        return result

    specs: list[ToolSpec] = [
        *read_tool_specs(),
        *([skill_spec()] if skills.on_demand else []),
        ToolSpec(SUBMIT_REVIEW, "Submit the review. Ends the run.", json_schema(ReviewResult)),
    ]
    handlers: dict[str, Handler] = read_handlers(ws) | {"read_skill": skill_handler(skills)}
    submits: dict[str, Submit[ReviewResult]] = {SUBMIT_REVIEW: submit}
    out = run_loop(model, system, context, user, specs, handlers, submits, max_turns, NUDGE)
    return ReviewOutcome(out.value, out.usage, out.turns)
