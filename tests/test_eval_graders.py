import json
from pathlib import Path
from typing import Any

import pytest

from evals.graders import Case, Check, grade, load_cases
from evals.harness import CostMeter, copy_fixture
from evals.judge import NUDGE, judge, load_calibration
from evals.summary import load, render
from specster.agent import AgentOutcome
from specster.llm.base import ToolCall, Usage
from specster.plan import normalize_plan
from specster.schemas import PlanTask, Question, QuestionsResult, SpecResult
from specster.workspace import Workspace
from tests.fakes import ScriptedModel

EN = "Add the CSV export to the report and write a test for it with the header."
ES = "Agrega la exportacion CSV para el informe con la cabecera y una prueba de que funciona."
RUBRIC = ["spec_is_implementable", "tasks_are_small_and_ordered"]


def spec_outcome(files: list[str], text: str = EN) -> AgentOutcome:
    task = PlanTask(id="a", title=text, description=text, files=files, acceptance=[text])
    result = SpecResult(
        title=text,
        objective=text,
        in_scope=[text],
        out_of_scope=[],
        files=files,
        approach=text,
        risks=[],
        test_strategy=text,
        tasks=[task],
    )
    tasks, fixes = normalize_plan(result.tasks)
    return AgentOutcome(result, tasks, fixes, Usage(), 3)


def questions_outcome(n: int) -> AgentOutcome:
    qs = [Question(question=f"Which format is bad ({i})?", why="It changes it.") for i in range(n)]
    result = QuestionsResult(summary="The exports are bad for the users.", questions=qs)
    return AgentOutcome(result, [], [], Usage(), 2)


def case(**overrides: Any) -> Case:
    return Case(**({"id": "c", "title": "t", "body": "b", "expect": "spec"} | overrides))


def oks(checks: list[Check]) -> dict[str, bool]:
    return {c.name: c.ok for c in checks}


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(copy_fixture(tmp_path / "repo"))


def test_right_decision_and_touched_files_pass(ws: Workspace) -> None:
    outcome = spec_outcome(["reports/export.py", "tests/test_export.py"])
    checks = grade(case(must_touch=["reports/export.py"]), outcome, ws, [])
    assert oks(checks) == {
        "decision": True,
        "language": True,
        "must_touch": True,
        "no_invented_dirs": True,
    }


def test_wrong_decision_fails(ws: Workspace) -> None:
    checks = grade(case(expect="questions"), spec_outcome(["reports/export.py"]), ws, [])
    decision = checks[0]
    assert (decision.name, decision.ok) == ("decision", False)
    assert decision.detail == "got spec, expected questions"


def test_missing_must_touch_is_named(ws: Workspace) -> None:
    checks = grade(
        case(must_touch=["reports/export.py", "tests/test_export.py"]),
        spec_outcome(["reports/export.py"]),
        ws,
        [],
    )
    must_touch = next(c for c in checks if c.name == "must_touch")
    assert not must_touch.ok
    assert "tests/test_export.py" in must_touch.detail


def test_new_file_in_an_existing_dir_is_fine_but_invented_dirs_fail(ws: Workspace) -> None:
    ok = grade(case(), spec_outcome(["reports/csv_export.py"]), ws, [])
    assert oks(ok)["no_invented_dirs"]
    bad = grade(case(), spec_outcome(["src/utils/helpers.py"]), ws, [])
    invented = next(c for c in bad if c.name == "no_invented_dirs")
    assert not invented.ok
    assert "src/utils/helpers.py" in invented.detail


def test_question_count_respects_the_case_cap(ws: Workspace) -> None:
    outcome = questions_outcome(3)
    assert oks(grade(case(expect="questions"), outcome, ws, []))["question_count"]
    assert not oks(grade(case(expect="questions", max_questions=2), outcome, ws, []))[
        "question_count"
    ]


def test_language_heuristic_tells_english_from_spanish(ws: Workspace) -> None:
    english, spanish = spec_outcome(["reports/export.py"]), spec_outcome(["reports/export.py"], ES)
    assert oks(grade(case(), english, ws, []))["language"]
    assert not oks(grade(case(language="es"), english, ws, []))["language"]
    assert oks(grade(case(language="es"), spanish, ws, []))["language"]
    assert not oks(grade(case(), spanish, ws, []))["language"]


def test_explored_before_submitting_needs_a_repo_tool_call(ws: Workspace) -> None:
    outcome = spec_outcome(["reports/export.py"])
    explore = case(must_explore=True)
    assert oks(grade(explore, outcome, ws, ["read_file", "submit_spec"]))[
        "explored_before_submitting"
    ]
    assert not oks(grade(explore, outcome, ws, ["submit_spec"]))["explored_before_submitting"]
    assert "explored_before_submitting" not in oks(grade(case(), outcome, ws, ["submit_spec"]))


def grade_call(**args: Any) -> list[ToolCall]:
    return [ToolCall("g1", "submit_grade", args)]


def test_judge_parses_the_grade() -> None:
    model = ScriptedModel(
        [grade_call(spec_is_implementable=5, tasks_are_small_and_ordered=3, reasons="ok")]
    )
    verdict = judge(model, RUBRIC, "thread", "{}")
    assert verdict.scores == {"spec_is_implementable": 5, "tasks_are_small_and_ordered": 3}
    assert verdict.reasons == "ok"
    assert not verdict.passed
    assert [t.name for t in model.tools] == ["submit_grade"]
    assert model.tools[0].parameters["required"] == [*RUBRIC, "reasons"]


def test_judge_nudges_once_after_a_text_reply() -> None:
    model = ScriptedModel(
        [
            "Let me think.",
            grade_call(spec_is_implementable=4, tasks_are_small_and_ordered=5, reasons="r"),
        ]
    )
    assert judge(model, RUBRIC, "thread", "{}").passed
    assert model.nudges == [NUDGE]


def test_judge_rejects_out_of_range_scores_with_a_tool_error() -> None:
    model = ScriptedModel(
        [
            grade_call(spec_is_implementable=9, tasks_are_small_and_ordered=5, reasons="r"),
            grade_call(spec_is_implementable=2, tasks_are_small_and_ordered=5, reasons="r"),
        ]
    )
    assert judge(model, RUBRIC, "thread", "{}").scores["spec_is_implementable"] == 2
    [error] = model.received[1]
    assert error.is_error
    assert error.call_id == "g1"


def test_judge_fails_after_two_misses() -> None:
    model = ScriptedModel(["no", "still no", "never sent"])
    with pytest.raises(RuntimeError, match="judge did not grade"):
        judge(model, RUBRIC, "thread", "{}")
    assert model.nudges == [NUDGE]
    assert model.script == ["never sent"]


def test_cost_meter_stops_once_over_budget() -> None:
    meter = CostMeter(1.0)
    meter.add(0.6)
    with pytest.raises(pytest.exit.Exception, match="eval budget reached") as stopped:
        meter.add(0.5)
    assert stopped.value.returncode == 3
    assert meter.spent == pytest.approx(1.1)


def test_cost_meter_treats_unknown_cost_as_over_budget_unless_allowed() -> None:
    with pytest.raises(pytest.exit.Exception, match="unknown cost"):
        CostMeter(100.0).add(None)
    allowed = CostMeter(100.0, allow_unknown=True)
    allowed.add(None)
    assert (allowed.spent, allowed.unknown_runs) == (0.0, 1)


def record(case_id: str, passed: bool, cost: float | None, failing: list[str]) -> dict[str, Any]:
    checks = [{"name": n, "ok": False, "detail": ""} for n in failing]
    return {
        "suite": "behavior",
        "case": case_id,
        "run": 0,
        "passed": passed,
        "checks": [*checks, {"name": "decision", "ok": True, "detail": ""}],
        "cost_usd": cost,
        "turns": 4,
    }


def test_summary_renders_one_row_per_case(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    rows = [
        record("clear", True, 0.01, []),
        record("clear", False, 0.03, ["must_touch"]),
        record("clear", False, None, ["must_touch", "language"]),
        record("vague", True, None, []),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    table = render(load(path)).splitlines()
    assert table[0].startswith("| suite | case | pass rate |")
    assert table[2] == (
        "| behavior | clear | 1/3 (33%) | $0.0200 (1 unknown) | 4.0 | language x1, must_touch x2 |"
    )
    assert table[3] == "| behavior | vague | 1/1 (100%) | unknown | 4.0 | - |"


def test_summary_without_results_says_so(tmp_path: Path) -> None:
    assert render(load(tmp_path / "missing.jsonl")) == "No eval results.\n"


def test_cases_load_and_validate() -> None:
    cases = load_cases()
    assert [c.id for c in cases] == [
        "clear-csv-export",
        "vague-export",
        "answerable-from-repo",
        "spanish-output",
    ]
    assert {c.language for c in cases} == {"en", "es"}
    with pytest.raises(ValueError, match="expect"):
        case(expect="maybe")


def test_calibration_samples_load_and_hold_valid_agent_outputs() -> None:
    samples = load_calibration()
    assert sorted((s.kind, s.label) for s in samples) == [
        ("questions", "bad"),
        ("questions", "good"),
        ("spec", "bad"),
        ("spec", "good"),
    ]
    for s in samples:
        model = SpecResult if s.kind == "spec" else QuestionsResult
        model.model_validate(s.output)
