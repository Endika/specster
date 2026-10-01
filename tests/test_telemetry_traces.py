import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from specster.config import ModelConfig
from specster.github import Issue
from specster.llm.base import ChatModel, ToolCall, ToolResult, Turn
from specster.llm.traced import TracedModel
from specster.run import main
from tests.fakes import FakeTracker, ScriptBook, ScriptedModel, bot_comment, spec_comment_body
from tests.test_approved import SPEC as BUILD_SPEC
from tests.test_approved import T0, TASK
from tests.test_run import SPEC, env, run, tracker
from tests.test_run_build import approve, book, go, is_reviewer, unprivileged, world

Spans = Callable[[], list[ReadableSpan]]


def one(found: list[ReadableSpan], name: str) -> ReadableSpan:
    named = [s for s in found if s.name == name]
    assert len(named) == 1, [s.name for s in found]
    return named[0]


def attrs(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def ancestors(span: ReadableSpan, found: list[ReadableSpan]) -> list[str]:
    by_id = {s.context.span_id: s for s in found if s.context is not None}
    names: list[str] = []
    parent = span.parent
    while parent is not None:
        up = by_id[parent.span_id]
        names.append(up.name)
        parent = up.parent
    return names


def test_a_spec_run_is_one_trace_with_chat_and_tool_spans(tmp_path: Path, spans: Spans) -> None:
    read = ToolCall("1", "read_file", {"path": "app.py"})
    model = ScriptedModel([[read], [ToolCall("2", "submit_spec", SPEC)]])
    assert run(env(tmp_path), tracker(), TracedModel(model, "fake", "fake-1")) == 0
    found = spans()
    assert len({s.context.trace_id for s in found if s.context is not None}) == 1
    root = one(found, "specster.run")
    assert root.parent is None
    assert attrs(root) == {
        "specster.repo": "o/r",
        "specster.phase": "spec",
        "specster.issue": 7,
        "specster.outcome": "spec",
    }
    chats = [s for s in found if s.name == "chat fake-1"]
    assert len(chats) == 2
    assert attrs(chats[0]) == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "fake",
        "gen_ai.request.model": "fake-1",
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 20,
        "specster.cache_read_tokens": 50,
        "specster.cache_write_tokens": 0,
    }
    tool = one(found, "tool read_file")
    assert attrs(tool) == {"specster.tool.error": False}
    assert ancestors(tool, found) == ["specster.run"]
    assert [s.name for s in found if "specster.issue" in attrs(s)] == ["specster.run"]


def test_parallel_tasks_hang_off_the_root(tmp_path: Path, spans: Spans) -> None:
    e, tr, _ = world(tmp_path, "  max_parallel: 2\n")
    second = TASK | {"id": "b", "files": ["b.py"]}
    tr.comments = [bot_comment(1, spec_comment_body(BUILD_SPEC | {"tasks": [TASK, second]}), T0)]
    write_b = ToolCall("1", "write_file", {"path": "b.py", "content": "B = 1\n"})
    done_b = ToolCall("2", "submit_task", {"summary": "B.", "commit_subject": "feat: add b"})
    worker = ScriptBook(
        {**book().scripts, 'id="b"': [[[write_b], [done_b]]]}, barrier=threading.Barrier(2)
    )
    assert go(e, tr, worker, approve()) == 0
    found = spans()
    assert [s.name for s in found if s.parent is None] == ["specster.run"]
    for name in ("task a", "task b"):
        assert ancestors(one(found, name), found)[-1] == "specster.run"
    assert len({s.context.trace_id for s in found if s.context is not None}) == 1


class ExplodingModel(ScriptedModel):
    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        raise RuntimeError("secret-ish text")


def test_a_failing_model_call_marks_the_span_without_its_message(
    tmp_path: Path, spans: Spans
) -> None:
    assert run(env(tmp_path), tracker(), TracedModel(ExplodingModel([]), "fake", "fake-1")) == 1
    found = spans()
    chat = one(found, "chat fake-1")
    assert chat.status.status_code is StatusCode.ERROR
    assert attrs(chat)["error.type"] == "RuntimeError"
    root = one(found, "specster.run")
    assert attrs(root)["specster.outcome"] == "error"
    assert root.status.status_code is StatusCode.ERROR
    assert root.status.description is None
    for s in found:
        assert "secret-ish" not in repr(attrs(s)) and "secret-ish" not in str(s.status.description)
        assert not s.events


def test_build_spans_cover_tests_review_and_rounds(tmp_path: Path, spans: Spans) -> None:
    e, tr, _ = world(tmp_path)
    assert go(e, tr, book(), approve()) == 0
    found = spans()
    assert attrs(one(found, "final_tests"))["specster.round"] == 0
    assert attrs(one(found, "review"))["specster.round"] == 0
    assert attrs(one(found, "task a")) == {
        "specster.task.id": "a",
        "specster.task.round": 0,
        "specster.task.escalated": False,
        "specster.task.status": "done",
    }
    one(found, "evidence")
    root = one(found, "specster.run")
    assert attrs(root)["specster.phase"] == "build"
    assert attrs(root)["specster.outcome"] == "pr_opened"


def test_an_escalated_task_gets_a_span_of_its_own(tmp_path: Path, spans: Spans) -> None:
    config = "models:\n  escalation: {provider: anthropic, model: claude-opus-5}\n"
    e, tr, _ = world(tmp_path, config)
    stuck = ScriptBook({'id="a"': [["thinking", "still thinking"]]})
    reviewer = approve()

    def make(cfg: ModelConfig) -> ChatModel:
        if cfg.model == "claude-opus-5":
            return book()
        return reviewer if is_reviewer(cfg) else stuck

    assert main(e, tr, make, lambda *_: b"", timer=lambda: 0.0, identity=unprivileged) == 0
    tasks = [attrs(s) for s in spans() if s.name == "task a"]
    assert [(t["specster.task.escalated"], t["specster.task.status"]) for t in tasks] == [
        (False, "failed"),
        (True, "done"),
    ]


def test_the_traced_model_hands_back_the_same_turns(spans: Spans) -> None:
    inner = ScriptedModel(["hello"])
    model = TracedModel(inner, "fake", "fake-1")
    assert (model.provider, model.model, model.inner) == ("fake", "fake-1", inner)
    turn = model.start("s", "c", "u", []).send()
    assert turn.text == "hello" and inner.received == [()]
    assert [s.name for s in spans()] == ["chat fake-1"]


class BrokenTracker(FakeTracker):
    def get_issue(self, number: int) -> Issue:
        raise LookupError("secret-ish text")


def test_an_unexpected_error_marks_the_root_without_its_message(
    tmp_path: Path, spans: Spans
) -> None:
    tr = tracker()
    broken = BrokenTracker(tr.issue, label_events=tr.label_events)
    assert run(env(tmp_path), broken, ScriptedModel([])) == 1
    root = one(spans(), "specster.run")
    assert root.status.status_code is StatusCode.ERROR and not root.events
    assert attrs(root)["error.type"] == "LookupError"
    assert attrs(root)["specster.outcome"] == "error"
    assert "secret-ish" not in repr(attrs(root)) + str(root.status.description)
