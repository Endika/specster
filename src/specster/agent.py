from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from specster.llm.base import ChatModel, ToolResult, ToolSpec, Usage
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
    def __init__(self, message: str, usage: Usage | None = None, turns: int = 0) -> None:
        super().__init__(message)
        self.usage = usage or Usage()
        self.turns = turns


type Handler = Callable[[dict[str, Any]], str]
type Submit[T] = Callable[[dict[str, Any]], T]
type _Accepted = tuple[QuestionsResult | SpecResult, list[PlanTask], list[str]]


class SubmissionError(Exception):
    pass


class NotYet(Exception):
    pass


@dataclass(frozen=True)
class LoopResult[T]:
    value: T
    usage: Usage
    turns: int


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


def read_tool_specs() -> list[ToolSpec]:
    return [
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
    ]


def read_handlers(ws: Workspace) -> dict[str, Handler]:
    return {
        "list_dir": lambda a: ws.list_dir(str(a.get("path", "."))),
        "read_file": lambda a: ws.read_file(
            str(a["path"]),
            int(a.get("start") or 1),
            int(a["end"]) if a.get("end") is not None else None,
        ),
        "grep": lambda a: ws.grep(str(a["pattern"]), str(a.get("path_glob", "**/*"))),
    }


def skill_spec() -> ToolSpec:
    return ToolSpec(
        "read_skill",
        "Read a project skill by name.",
        _obj({"name": {"type": "string"}}, ["name"]),
    )


def skill_handler(skills: SkillBook) -> Handler:
    return lambda a: skills.get(str(a["name"])).body


def tool_specs(has_on_demand_skills: bool) -> list[ToolSpec]:
    return [
        *read_tool_specs(),
        *([skill_spec()] if has_on_demand_skills else []),
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


_USAGE_ATTR = "specster_usage"


def attach_usage(e: BaseException, usage: Usage, turns: int) -> None:
    """Carry the spend so far on a fatal error, so the caller can still bill it."""
    setattr(e, _USAGE_ATTR, (usage, turns))


def attached_usage(e: BaseException) -> tuple[Usage, int] | None:
    found = getattr(e, _USAGE_ATTR, None)
    return found if isinstance(found, tuple) else None


def run_loop[T](
    model: ChatModel,
    system: str,
    context: str,
    user: str,
    tools: Sequence[ToolSpec],
    handlers: Mapping[str, Handler],
    submits: Mapping[str, Submit[T]],
    max_turns: int,
    nudge: str,
) -> LoopResult[T]:
    usage = Usage()
    try:
        session = model.start(system, context, user, tools)
    except Exception as e:
        raise AgentError(f"{type(e).__name__}: {e}") from e
    results: Sequence[ToolResult] = ()
    note: str | None = None
    text_only = bad = 0
    for turn_no in range(1, max_turns + 1):
        if turn_no == max_turns and note is None and turn_no > 1:
            note = LAST
        try:
            turn = session.send(results, note)
        except Exception as e:
            raise AgentError(f"{type(e).__name__}: {e}", usage, turn_no - 1) from e
        usage = usage + turn.usage
        note = None
        if not turn.tool_calls:
            text_only += 1
            if text_only >= 2:
                raise AgentError("model stopped without submitting", usage, turn_no)
            results, note = (), nudge
            continue
        text_only = 0
        out: list[ToolResult] = []
        try:
            for call in turn.tool_calls:
                if call.raw_arguments is not None:
                    out.append(ToolResult(call.id, "arguments were not valid JSON", True))
                    continue
                if call.name in submits:
                    try:
                        return LoopResult(submits[call.name](call.arguments), usage, turn_no)
                    except NotYet as e:
                        out.append(ToolResult(call.id, str(e), True))
                    except (ValidationError, PlanError, SubmissionError) as e:
                        bad += 1
                        if bad >= 2:
                            raise AgentError(
                                f"invalid submission twice: {e}", usage, turn_no
                            ) from e
                        message = f"Invalid submission: {e}. Fix it and submit again."
                        out.append(ToolResult(call.id, message, True))
                    continue
                handler = handlers.get(call.name)
                if handler is None:
                    out.append(ToolResult(call.id, f"unknown tool {call.name}", True))
                    continue
                try:
                    out.append(ToolResult(call.id, handler(call.arguments)))
                except (ToolError, KeyError, ValueError, TypeError, OSError) as e:
                    out.append(ToolResult(call.id, str(e), True))
        except AgentError:
            raise
        except BaseException as e:
            attach_usage(e, usage, turn_no)
            raise
        results = out
    raise AgentError(f"no submission after {max_turns} turns", usage, max_turns)


def run_agent(
    model: ChatModel,
    system: str,
    context: str,
    thread_text: str,
    ws: Workspace,
    skills: SkillBook,
    max_turns: int,
    max_questions: int = 5,
    *,
    revision: bool = False,
) -> AgentOutcome:
    def questions(args: dict[str, Any]) -> _Accepted:
        result = QuestionsResult.model_validate(args)
        if len(result.questions) > max_questions:
            raise SubmissionError(
                f"{len(result.questions)} questions; ask at most {max_questions}, the ones "
                "that change the spec most"
            )
        return result, [], []

    def spec(args: dict[str, Any]) -> _Accepted:
        result = SpecResult.model_validate(args)
        if result.changes and not revision:
            raise SubmissionError("changes is only for revising a previous spec; leave it empty")
        tasks, fixes = normalize_plan(result.tasks)
        return result, tasks, fixes

    handlers = read_handlers(ws) | {"read_skill": skill_handler(skills)}
    submits: dict[str, Submit[_Accepted]] = {SUBMIT_QUESTIONS: questions, SUBMIT_SPEC: spec}
    tools = tool_specs(bool(skills.on_demand))
    out = run_loop(model, system, context, thread_text, tools, handlers, submits, max_turns, NUDGE)
    result, tasks, fixes = out.value
    return AgentOutcome(result, tasks, fixes, out.usage, out.turns)
