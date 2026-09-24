import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from specster.agent import (
    AgentError,
    Handler,
    NotYet,
    Submit,
    read_handlers,
    read_tool_specs,
    run_loop,
    skill_handler,
    skill_spec,
)
from specster.closing import closes_an_issue, drop_references
from specster.llm.base import ChatModel, ChatSession, ToolResult, ToolSpec, Turn, Usage
from specster.sandbox import RunResult, Sandbox, SandboxError
from specster.schemas import SUBMIT_TASK, PlanTask, TaskSubmission, json_schema
from specster.skills import SkillBook
from specster.workspace import TaskWorkspace, ToolError

NUDGE = "Call submit_task now."
ONE_RUN = "one test run per turn; call run_tests again next turn"
CHANGED_SINCE = "tests already ran this turn and files changed since; call submit_task next turn"
TIME_UP = "the build reached its time limit (build.max_minutes)"
NO_TESTS = "No test command is configured (build.test_command), so nothing runs your code."
SUBJECT_MAX = 72
_SUBJECT = re.compile(
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*$"
)


@dataclass(frozen=True)
class WorkerResult:
    task_id: str
    status: Literal["done", "failed", "not_started"]
    reason: str
    summary: str
    subject: str
    changes: dict[str, str]
    originals: dict[str, bytes | None]
    usage: Usage
    turns: int
    test_runs: int
    tests_passed: bool | None
    truncations: list[str]


def _tool_text(res: RunResult) -> str:
    timed_out = " (timed out)" if res.timed_out else ""
    note = f"[{res.truncation}]\n" if res.truncation else ""
    return f"exit {res.exit_code}{timed_out}\n{note}{res.output}"


class TimeUp(Exception):
    """The build's wall-clock limit passed: the worker stops before another paid turn."""


class TaskTools:
    def __init__(
        self,
        ws: TaskWorkspace,
        sandbox: Sandbox,
        home: Path,
        setup: Sequence[str] | None,
        test: Sequence[str] | None,
        task_id: str,
        time_left: Callable[[], float] | None = None,
    ) -> None:
        self.ws = ws
        self.sandbox = sandbox
        self.home = home
        self.setup = list(setup) if setup else None
        self.test = list(test) if test else None
        self.task_id = task_id
        self.time_left = time_left
        self.runs = 0
        self.last: RunResult | None = None
        self.truncations: list[str] = []
        self._set_up = False
        # This turn's run: the workspace write count it saw, whether it passed, its tool text.
        self._turn_run: tuple[int, bool, str] | None = None
        # The latest run of any turn, in the same form.
        self._last_run: tuple[int, bool, str] | None = None

    def new_turn(self) -> None:
        self._turn_run = None

    def out_of_time(self) -> bool:
        return self.time_left is not None and self.time_left() <= 0

    def _run(self, argv: Sequence[str], label: str) -> RunResult:
        try:
            self.sandbox.hand_over(self.ws.root)
        except OSError as e:
            raise SandboxError(f"could not hand {self.ws.root} over: {e}") from e
        res = self.sandbox.run(argv, self.ws.root, self.home, label)
        if res.truncation:
            self.truncations.append(res.truncation)
        return res

    def run_tests(self) -> str:
        """A SandboxError propagates: it is fatal for the build."""
        if self.test is None:
            return NO_TESTS
        if self._turn_run is not None:
            raise ToolError(ONE_RUN)
        seen = self.ws.writes
        passed, text = self._tests(self.test)
        self._turn_run = self._last_run = (seen, passed, text)
        return text

    def _tests(self, test: Sequence[str]) -> tuple[bool, str]:
        if self.setup is not None and not self._set_up:
            res = self._run(self.setup, f"setup {self.task_id}")
            if not res.ok:
                failed = f"The setup command failed, so the tests did not run.\n{_tool_text(res)}"
                return False, failed
            self._set_up = True
        res = self._run(test, f"tests {self.task_id}")
        self.runs += 1
        self.last = res
        return res.ok, _tool_text(res)

    def submit(self, args: dict[str, Any]) -> TaskSubmission:
        submission = TaskSubmission.model_validate(args)
        if self.test is None:
            return submission
        last = self._last_run
        if self._turn_run is None and last is not None and last[0] == self.ws.writes and last[1]:
            # An earlier turn's passing run already saw exactly these files.
            return submission
        if self._turn_run is None:
            self.run_tests()
        seen, passed, text = self._turn_run or (self.ws.writes, False, "")
        if seen != self.ws.writes:
            raise NotYet(CHANGED_SINCE)
        if not passed:
            raise NotYet(f"Tests fail, so the task is not done yet:\n{text}")
        return submission


class _TurnSession:
    def __init__(self, session: ChatSession, tools: TaskTools) -> None:
        self._session = session
        self._tools = tools

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        if self._tools.out_of_time():
            raise TimeUp(TIME_UP)
        turn = self._session.send(results, user_text)
        self._tools.new_turn()
        return turn


class _TurnModel:
    """Tells the tools where each model turn starts, for the one-test-run-per-turn rule."""

    def __init__(self, model: ChatModel, tools: TaskTools) -> None:
        self.provider = model.provider
        self.model = model.model
        self._inner = model
        self._tools = tools

    def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> ChatSession:
        return _TurnSession(self._inner.start(system, context, user, tools), self._tools)


def valid_subject(subject: str) -> bool:
    return (
        len(subject) <= SUBJECT_MAX
        and subject.isprintable()
        and _SUBJECT.fullmatch(subject) is not None
        and not closes_an_issue(subject.replace("`", ""))
    )


def fallback_subject(task: PlanTask, correction: bool) -> tuple[str, str | None]:
    """A valid subject from the plan; a planner's title can carry control characters."""
    if correction:
        return f"fix({task.id}): address review findings", None
    printable = "".join(c if c.isprintable() else " " for c in task.title)
    title = " ".join(drop_references(printable).split())
    prefix, note = f"feat({task.id}): ", None
    room = SUBJECT_MAX - len(prefix)
    if len(title) > room:
        title = title[:room].rstrip()
        note = f"commit subject for {task.id} cut to 72 characters"
    subject = prefix + title
    if not valid_subject(subject):
        return f"feat({task.id}): implement the task", None
    return subject, note


def _write_specs() -> list[ToolSpec]:
    def obj(props: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        }

    path = {"type": "string", "description": "Repo-relative path of a file of this task."}
    return [
        ToolSpec(
            "write_file",
            "Write the whole content of a file of this task, creating it if needed.",
            obj({"path": path, "content": {"type": "string"}}),
        ),
        ToolSpec(
            "edit_file",
            "Replace old with new in a file of this task; old must appear exactly once.",
            obj({"path": path, "old": {"type": "string"}, "new": {"type": "string"}}),
        ),
        ToolSpec("run_tests", "Run the repository's test command in a sandbox.", obj({})),
        ToolSpec(
            SUBMIT_TASK,
            "Submit the finished task. Refused while the tests fail.",
            json_schema(TaskSubmission),
        ),
    ]


def run_worker(
    model: ChatModel,
    system: str,
    context: str,
    user: str,
    tools: TaskTools,
    task: PlanTask,
    skills: SkillBook,
    max_turns: int,
    correction: bool,
) -> WorkerResult:
    """A SandboxError propagates; any AgentError is a failed task."""
    ws = tools.ws
    specs = [*read_tool_specs(), *([skill_spec()] if skills.on_demand else []), *_write_specs()]
    handlers: dict[str, Handler] = read_handlers(ws) | {
        "read_skill": skill_handler(skills),
        "write_file": lambda a: ws.write_file(str(a["path"]), str(a["content"])),
        "edit_file": lambda a: ws.edit_file(str(a["path"]), str(a["old"]), str(a["new"])),
        "run_tests": lambda _: tools.run_tests(),
    }
    submits: dict[str, Submit[TaskSubmission]] = {SUBMIT_TASK: tools.submit}

    def result(
        status: Literal["done", "failed"],
        reason: str,
        summary: str,
        subject: str,
        note: str | None,
        usage: Usage,
        turns: int,
    ) -> WorkerResult:
        passed = None if tools.test is None else tools.last is not None and tools.last.ok
        return WorkerResult(
            task.id,
            status,
            reason,
            summary,
            subject,
            dict(ws.changes),
            dict(ws.originals),
            usage,
            turns,
            tools.runs,
            passed,
            [*ws.truncations, *tools.truncations, *([note] if note else [])],
        )

    try:
        counted = _TurnModel(model, tools)
        out = run_loop(counted, system, context, user, specs, handlers, submits, max_turns, NUDGE)
    except AgentError as e:
        reason = str(e)
        if tools.last is not None and not tools.last.ok:
            reason += "; tests still fail"
        return result("failed", reason, "", "", None, e.usage, e.turns)
    sub = out.value
    if valid_subject(sub.commit_subject):
        subject, note = sub.commit_subject, None
    else:
        subject, note = fallback_subject(task, correction)
    return result("done", "", sub.summary, subject, note, out.usage, out.turns)
