from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from specster.llm.base import ChatModel, ToolCall, ToolResult, ToolSpec, Usage
from specster.plan import PlanError, normalize_plan
from specster.schemas import (
    SUBMIT_QUESTIONS,
    SUBMIT_SPEC,
    PlanTask,
    QuestionsResult,
    SpecResult,
    json_schema,
)
from specster.skills import SkillBook
from specster.workspace import ToolError, Workspace

NUDGE = "Call submit_questions or submit_spec now."
LAST = "Last turn: submit now."


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class AgentOutcome:
    result: QuestionsResult | SpecResult
    tasks: list[PlanTask]
    plan_fixes: list[str]
    usage: Usage
    turns: int


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def tool_specs(has_on_demand_skills: bool) -> list[ToolSpec]:
    specs = [
        ToolSpec(
            "list_dir",
            "List a repository directory.",
            _obj({"path": {"type": "string", "description": "Repo-relative, default '.'"}}, []),
        ),
        ToolSpec(
            "read_file",
            "Read numbered lines of a repository file (max 400 per call).",
            _obj(
                {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                ["path"],
            ),
        ),
        ToolSpec(
            "grep",
            "Search repository files with a Python regex.",
            _obj(
                {
                    "pattern": {"type": "string"},
                    "path_glob": {"type": "string", "description": "gitignore-style glob"},
                },
                ["pattern"],
            ),
        ),
        ToolSpec(
            SUBMIT_QUESTIONS,
            "Submit clarifying questions. Ends the run.",
            json_schema(QuestionsResult),
        ),
        ToolSpec(
            SUBMIT_SPEC,
            "Submit the technical spec and task plan. Ends the run.",
            json_schema(SpecResult),
        ),
    ]
    if has_on_demand_skills:
        specs.insert(
            3,
            ToolSpec(
                "read_skill",
                "Read a project skill by name.",
                _obj({"name": {"type": "string"}}, ["name"]),
            ),
        )
    return specs


def _dispatch(call: ToolCall, ws: Workspace, skills: SkillBook) -> str:
    a = call.arguments
    handlers: dict[str, Callable[[], str]] = {
        "list_dir": lambda: ws.list_dir(str(a.get("path", "."))),
        "read_file": lambda: ws.read_file(
            str(a["path"]),
            int(a.get("start", 1)),
            int(a["end"]) if a.get("end") is not None else None,
        ),
        "grep": lambda: ws.grep(str(a["pattern"]), str(a.get("path_glob", "**/*"))),
        "read_skill": lambda: skills.get(str(a["name"])).body,
    }
    if call.name not in handlers:
        raise ToolError(f"unknown tool {call.name}")
    return handlers[call.name]()


def run_agent(
    model: ChatModel,
    system: str,
    context: str,
    thread_text: str,
    ws: Workspace,
    skills: SkillBook,
    max_turns: int,
) -> AgentOutcome:
    session = model.start(system, context, thread_text, tool_specs(bool(skills.on_demand)))
    usage = Usage()
    results: Sequence[ToolResult] = ()
    nudge: str | None = None
    text_only = 0
    bad_submissions = 0
    for turn_no in range(1, max_turns + 1):
        if turn_no == max_turns and nudge is None and turn_no > 1:
            nudge = LAST
        turn = session.send(results, nudge)
        usage = usage + turn.usage
        nudge = None
        if not turn.tool_calls:
            text_only += 1
            if text_only >= 2:
                raise AgentError("model stopped without submitting")
            results, nudge = (), NUDGE
            continue
        text_only = 0
        out: list[ToolResult] = []
        for call in turn.tool_calls:
            if call.raw_arguments is not None:
                out.append(ToolResult(call.id, "arguments were not valid JSON", True))
                continue
            if call.name in (SUBMIT_QUESTIONS, SUBMIT_SPEC):
                try:
                    return _accept(call, usage, turn_no)
                except (ValidationError, PlanError) as e:
                    bad_submissions += 1
                    if bad_submissions >= 2:
                        raise AgentError(f"invalid submission twice: {e}") from e
                    message = f"Invalid submission: {e}. Fix it and submit again."
                    out.append(ToolResult(call.id, message, True))
                continue
            try:
                out.append(ToolResult(call.id, _dispatch(call, ws, skills)))
            except (ToolError, KeyError, ValueError) as e:
                out.append(ToolResult(call.id, str(e), True))
        results = out
    raise AgentError(f"no submission after {max_turns} turns")


def _accept(call: ToolCall, usage: Usage, turns: int) -> AgentOutcome:
    if call.name == SUBMIT_QUESTIONS:
        return AgentOutcome(QuestionsResult.model_validate(call.arguments), [], [], usage, turns)
    spec = SpecResult.model_validate(call.arguments)
    tasks, fixes = normalize_plan(spec.tasks)
    return AgentOutcome(spec, tasks, fixes, usage, turns)
