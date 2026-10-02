from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from specster.agent import AgentError, Metered, NotYet, SubmissionError, run_agent, run_loop
from specster.config import ModelConfig, SkillsConfig
from specster.ledger import Ledger
from specster.llm.base import ModelRefusal, ToolCall, ToolResult, ToolSpec, Turn, Usage
from specster.schemas import EvidencePage, EvidenceRequest, QuestionsResult, SpecResult
from specster.skills import SkillBook, load_skills
from specster.workspace import Workspace
from tests.fakes import ScriptedModel

SPEC = {
    "title": "CSV export",
    "objective": "o",
    "in_scope": ["a"],
    "out_of_scope": [],
    "files": ["app.py"],
    "approach": "p",
    "risks": [],
    "test_strategy": "t",
    "closing_line": "Boo.",
    "tasks": [
        {"id": "a", "title": "A", "description": "d", "files": ["app.py"], "acceptance": ["x"]},
        {"id": "b", "title": "B", "description": "d", "files": ["app.py"], "acceptance": ["y"]},
    ],
}


def setup(tmp_path: Path) -> tuple[Workspace, SkillBook]:
    (tmp_path / "app.py").write_text("def export():\n    pass\n")
    (tmp_path / "AGENTS.md").write_text("Use uv.")
    ws = Workspace(tmp_path)
    return ws, load_skills(tmp_path, SkillsConfig(), "spec", lambda *_: b"", None)


def test_explores_then_submits_a_spec_with_normalized_plan(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = ScriptedModel(
        [
            [
                ToolCall("1", "grep", {"pattern": "def export"}),
                ToolCall("2", "read_skill", {"name": "AGENTS"}),
            ],
            [ToolCall("3", "submit_spec", SPEC)],
        ]
    )
    out = run_agent(model, "sys", "ctx", "thread", ws, skills, max_turns=5)
    assert out.turns == 2
    assert [r.content for r in model.received[1]] == ["app.py:1: def export():", "Use uv."]
    assert out.tasks[1].depends_on == ["a"]
    assert out.plan_fixes == ["b now runs after a: both touch app.py"]
    assert out.usage.input_tokens == 200
    assert skills.read == {"AGENTS"}


def test_invalid_submission_gets_one_retry_with_the_error(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    bad = SPEC | {"tasks": []}
    model = ScriptedModel(
        [[ToolCall("1", "submit_spec", bad)], [ToolCall("2", "submit_spec", SPEC)]]
    )
    out = run_agent(model, "s", "c", "t", ws, skills, max_turns=5)
    assert model.received[1][0].is_error and "tasks" in model.received[1][0].content
    assert isinstance(out.result, SpecResult) and out.result.title == "CSV export"


def test_two_invalid_submissions_fail(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    bad = SPEC | {"tasks": []}
    model = ScriptedModel(
        [[ToolCall("1", "submit_spec", bad)], [ToolCall("2", "submit_spec", bad)]]
    )
    with pytest.raises(AgentError, match="invalid submission twice"):
        run_agent(model, "s", "c", "t", ws, skills, max_turns=5)


def test_tool_errors_are_returned_to_the_model_not_raised(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = ScriptedModel(
        [
            [
                ToolCall("1", "read_file", {"path": "../secret"}),
                ToolCall("2", "nope", {}),
                ToolCall("3", "grep", {}, raw_arguments="{bad"),
            ],
            [
                ToolCall(
                    "4",
                    "submit_questions",
                    {"summary": "s", "questions": [{"question": "q", "why": "w"}]},
                )
            ],
        ]
    )
    run_agent(model, "s", "c", "t", ws, skills, max_turns=5)
    results = model.received[1]
    assert all(r.is_error for r in results)
    assert "outside" in results[0].content and "unknown tool" in results[1].content
    assert "not valid JSON" in results[2].content


def test_text_only_turn_is_nudged_once_then_fails(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = ScriptedModel(["thinking out loud", "still talking"])
    with pytest.raises(AgentError, match="without submitting"):
        run_agent(model, "s", "c", "t", ws, skills, max_turns=5)
    assert model.nudges == ["Call submit_questions or submit_spec now."]


def test_bad_start_argument_types_do_not_crash_the_loop(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = ScriptedModel(
        [
            [
                ToolCall("1", "read_file", {"path": "app.py", "start": None}),
                ToolCall("2", "read_file", {"path": "app.py", "start": [1]}),
            ],
            [
                ToolCall(
                    "3",
                    "submit_questions",
                    {"summary": "s", "questions": [{"question": "q", "why": "w"}]},
                )
            ],
        ]
    )
    run_agent(model, "s", "c", "t", ws, skills, max_turns=5)
    results = model.received[1]
    assert results[0].is_error is False  # start: None falls back to the default
    assert results[1].is_error is True  # start: [1] is a genuine type error, reported back


def test_turn_cap_warns_on_last_turn_then_fails(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = ScriptedModel([[ToolCall(str(i), "list_dir", {})] for i in range(3)])
    with pytest.raises(AgentError, match="after 3 turns") as err:
        run_agent(model, "s", "c", "t", ws, skills, max_turns=3)
    assert model.nudges == ["Last turn: submit now."]
    assert err.value.usage == Usage(300, 150, 0, 60) and err.value.turns == 3


class RefusesOnSecondTurn(ScriptedModel):
    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        if self.received:
            raise ModelRefusal("declined")
        return super().send(results, user_text)


def test_provider_failure_mid_run_keeps_the_usage_so_far(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    model = RefusesOnSecondTurn([[ToolCall("1", "list_dir", {})]])
    with pytest.raises(AgentError, match="ModelRefusal: declined") as err:
        run_agent(model, "s", "c", "t", ws, skills, max_turns=5)
    assert isinstance(err.value.__cause__, ModelRefusal)
    assert err.value.usage == Usage(100, 50, 0, 20) and err.value.turns == 1


def test_more_questions_than_allowed_are_sent_back_once(tmp_path: Path) -> None:
    ws, skills = setup(tmp_path)
    q = {"question": "q", "why": "w"}
    too_many = {"summary": "s", "questions": [q, q, q, q]}
    model = ScriptedModel(
        [
            [ToolCall("1", "submit_questions", too_many)],
            [ToolCall("2", "submit_questions", {"summary": "s", "questions": [q, q]})],
        ]
    )
    out = run_agent(model, "s", "c", "t", ws, skills, max_turns=5, max_questions=3)
    sent_back = model.received[1][0]
    assert sent_back.is_error and "ask at most 3" in sent_back.content
    assert isinstance(out.result, QuestionsResult) and len(out.result.questions) == 2


SUBMIT_X = [ToolSpec("submit_x", "Submit.", {"type": "object", "properties": {}})]


def test_not_yet_is_sent_back_without_counting_as_an_invalid_submission() -> None:
    calls: list[dict[str, Any]] = []

    def submit(args: dict[str, Any]) -> str:
        calls.append(args)
        if len(calls) < 3:
            raise NotYet("tests fail: exit 1")
        return "done"

    model = ScriptedModel([[ToolCall(str(i), "submit_x", {})] for i in range(3)])
    out = run_loop(
        model, "s", "c", "u", SUBMIT_X, {}, {"submit_x": submit}, 5, "Call submit_x now."
    )
    assert (out.value, out.turns, out.usage.input_tokens) == ("done", 3, 300)
    assert [(r.content, r.is_error) for r in model.received[1]] == [("tests fail: exit 1", True)]


def test_two_invalid_submissions_end_the_loop() -> None:
    def submit(_args: dict[str, Any]) -> str:
        raise SubmissionError("bad subject")

    model = ScriptedModel([[ToolCall(str(i), "submit_x", {})] for i in range(2)])
    with pytest.raises(AgentError, match="invalid submission twice: bad subject"):
        run_loop(model, "s", "c", "u", SUBMIT_X, {}, {"submit_x": submit}, 5, "n")


def test_the_nudge_is_the_callers_and_handlers_answer_other_tools() -> None:
    model = ScriptedModel(
        ["thinking", [ToolCall("1", "echo", {"v": "hi"})], [ToolCall("2", "submit_x", {})]]
    )
    handlers = {"echo": lambda a: str(a["v"])}
    out = run_loop(
        model,
        "s",
        "c",
        "u",
        SUBMIT_X,
        handlers,
        {"submit_x": lambda _a: 1},
        5,
        "Call submit_x now.",
    )
    assert out.value == 1 and model.nudges == ["Call submit_x now."]
    assert model.received[2][0].content == "hi"


def test_metered_stops_before_the_turn_once_spent() -> None:
    checks = 0

    def spent() -> str | None:
        nonlocal checks
        checks += 1
        return "build budget spent: $1.00 of $1.00" if checks == 3 else None

    ledger = Ledger({})
    meter = ledger.meter("worker", ModelConfig(model="claude-sonnet-5"))
    model = ScriptedModel([[ToolCall(str(i), "echo", {})] for i in range(5)])
    with pytest.raises(AgentError, match=r"BudgetSpent: build budget spent: \$1\.00") as err:
        run_loop(
            Metered(model, meter, spent),
            "s",
            "c",
            "u",
            SUBMIT_X,
            {"echo": lambda _a: ""},
            {},
            5,
            "n",
        )
    assert len(model.received) == 2 and err.value.turns == 2
    assert ledger.known_cost() > 0
    meter.close()
    assert ledger.known_cost() == 0


EVIDENCE = [{"name": "list-users", "method": "GET", "path": "/users?limit=2", "why": "New field."}]


def plan_with(
    tmp_path: Path, submissions: list[dict[str, Any]], preview: bool
) -> tuple[Any, ScriptedModel]:
    ws, skills = setup(tmp_path)
    model = ScriptedModel([[ToolCall(str(i), "submit_spec", s)] for i, s in enumerate(submissions)])
    return run_agent(model, "s", "c", "t", ws, skills, max_turns=5, preview=preview), model


def test_spec_with_evidence_is_refused_without_preview(tmp_path: Path) -> None:
    out, model = plan_with(tmp_path, [SPEC | {"evidence": EVIDENCE}, SPEC], preview=False)
    assert out.result.evidence == []
    assert model.received[1][0].is_error and "build.preview" in model.received[1][0].content


def test_spec_keeps_evidence_with_preview(tmp_path: Path) -> None:
    out, _ = plan_with(tmp_path, [SPEC | {"evidence": EVIDENCE}], preview=True)
    assert [e.name for e in out.result.evidence] == ["list-users"]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "Bad Name", "method": "GET", "path": "/", "why": "x"},
        {"name": "a", "method": "TRACE", "path": "/", "why": "x"},
        {"name": "a", "method": "GET", "path": "http://evil.test/", "why": "x"},
        {"name": "a", "method": "GET", "path": "//evil.test/", "why": "x"},
        {"name": "a", "method": "GET", "path": "/ space", "why": "x"},
    ],
)
def test_evidence_request_rejects(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceRequest.model_validate(bad)


def test_an_evidence_body_too_long_for_the_spec_comment_is_refused(tmp_path: Path) -> None:
    big = EVIDENCE[0] | {"method": "POST", "body": {"rows": ["x" * 100] * 20}}
    out, model = plan_with(tmp_path, [SPEC | {"evidence": [big]}, SPEC], preview=True)
    assert out.result.evidence == []
    refusal = model.received[1][0]
    assert refusal.is_error and "list-users" in refusal.content and "2,000" in refusal.content
    fits = EVIDENCE[0] | {"method": "POST", "body": {"rows": ["x" * 100] * 10}}
    (tmp_path / "fits").mkdir()
    out, _ = plan_with(tmp_path / "fits", [SPEC | {"evidence": [fits]}], preview=True)
    assert out.result.evidence[0].body == {"rows": ["x" * 100] * 10}


def test_evidence_names_must_be_unique(tmp_path: Path) -> None:
    twice = SPEC | {"evidence": EVIDENCE * 2}
    out, model = plan_with(tmp_path, [twice, SPEC | {"evidence": EVIDENCE}], preview=True)
    assert len(out.result.evidence) == 1
    assert "unique" in model.received[1][0].content


PAGES = [{"name": "users-page", "path": "/users", "why": "New column."}]


def test_spec_with_pages_is_refused_without_preview(tmp_path: Path) -> None:
    out, model = plan_with(tmp_path, [SPEC | {"pages": PAGES}, SPEC], preview=False)
    assert out.result.pages == []
    assert model.received[1][0].is_error and "build.preview" in model.received[1][0].content


def test_spec_keeps_pages_with_preview(tmp_path: Path) -> None:
    out, _ = plan_with(tmp_path, [SPEC | {"pages": PAGES}], preview=True)
    assert [p.name for p in out.result.pages] == ["users-page"]


def test_a_name_cannot_repeat_between_evidence_and_pages(tmp_path: Path) -> None:
    clash = SPEC | {"evidence": EVIDENCE, "pages": [PAGES[0] | {"name": "list-users"}]}
    out, model = plan_with(tmp_path, [clash, SPEC | {"pages": PAGES}], preview=True)
    assert len(out.result.pages) == 1
    assert "unique" in model.received[1][0].content


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "Bad Name", "path": "/", "why": "x"},
        {"name": "a", "path": "http://evil.test/", "why": "x"},
        {"name": "a", "path": "//evil.test/", "why": "x"},
        {"name": "a", "path": "/#frag", "why": "x"},
        {"name": "a", "path": "/", "why": "x", "method": "GET"},
    ],
)
def test_evidence_page_rejects(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidencePage.model_validate(bad)


def test_evidence_and_pages_share_the_maximum_of_ten() -> None:
    def page(i: int) -> dict[str, str]:
        return {"name": f"p{i}", "path": "/", "why": "x"}

    ev = [EVIDENCE[0] | {"name": f"e{i}"} for i in range(5)]
    SpecResult.model_validate(SPEC | {"evidence": ev, "pages": [page(i) for i in range(5)]})
    with pytest.raises(ValidationError, match="together"):
        SpecResult.model_validate(SPEC | {"evidence": ev, "pages": [page(i) for i in range(6)]})
