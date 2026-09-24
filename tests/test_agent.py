from collections.abc import Sequence
from pathlib import Path

import pytest

from specster.agent import AgentError, run_agent
from specster.config import SkillsConfig
from specster.llm.base import ModelRefusal, ToolCall, ToolResult, Turn, Usage
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
    assert out.result.title == "CSV export"  # type: ignore[union-attr]


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
