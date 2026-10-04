import json
from typing import Any

import pytest
from pydantic import ValidationError

from specster.schemas import PlanTask, QuestionsResult, ReviewResult, SpecResult, json_schema


def test_schema_has_no_refs_so_every_provider_can_read_it() -> None:
    text = json.dumps(json_schema(SpecResult))
    assert "$ref" not in text
    assert "$defs" not in text
    props = json_schema(SpecResult)["properties"]
    assert props["tasks"]["items"]["properties"]["depends_on"]["type"] == "array"


def test_spec_result_keeps_its_title_property() -> None:
    assert "title" in json_schema(SpecResult)["properties"]


def test_task_id_must_be_a_slug() -> None:
    with pytest.raises(ValidationError):
        PlanTask(id="Task 1", title="t", description="d", files=["a.py"], acceptance=["x"])


def test_questions_are_capped_at_five() -> None:
    q = {"question": "q", "why": "w"}
    with pytest.raises(ValidationError):
        QuestionsResult.model_validate({"summary": "s", "questions": [q] * 6})


def test_runaway_text_is_rejected_so_the_model_gets_it_back() -> None:
    with pytest.raises(ValidationError):
        QuestionsResult.model_validate(
            {"summary": "s", "questions": [{"question": "x" * 401, "why": "w"}]}
        )


def test_a_review_carries_at_most_thirty_findings() -> None:
    finding = {"task_id": "a", "file": "app.py", "severity": "minor", "description": "d"}
    assert (
        len(
            ReviewResult.model_validate({"verdict": "approve", "findings": [finding] * 30}).findings
        )
        == 30
    )
    with pytest.raises(ValidationError, match="at most 30 items"):
        ReviewResult.model_validate({"verdict": "approve", "findings": [finding] * 31})
    assert json_schema(ReviewResult)["properties"]["findings"]["maxItems"] == 30


def test_the_long_markdown_fields_ask_for_real_line_breaks_not_json_escapes() -> None:
    props = json_schema(SpecResult)["properties"]
    for name in ("approach", "test_strategy"):
        assert "real line breaks" in props[name]["description"], name


TASK: dict[str, Any] = {
    "id": "a",
    "title": "A",
    "description": "d",
    "files": ["app.py"],
    "acceptance": ["x"],
}
SPEC: dict[str, Any] = {
    "title": "t",
    "objective": "o",
    "in_scope": ["a"],
    "out_of_scope": [],
    "files": ["app.py"],
    "approach": "p",
    "risks": [],
    "test_strategy": "t",
    "tasks": [
        {"id": "a", "title": "A", "description": "d", "files": ["app.py"], "acceptance": ["x"]}
    ],
}


@pytest.mark.parametrize(
    "text",
    [
        "- one\n- two",
        "* one\n\n* two\n",
        f"{chr(0x2022)} one\n{chr(0x2022)} two",
        "1. one\n2) two",
        "  - one\n  - two  ",
    ],
)
def test_a_list_sent_as_bulleted_text_becomes_a_list(text: str) -> None:
    assert SpecResult.model_validate(SPEC | {"risks": text}).risks == ["one", "two"]


def test_a_single_line_or_an_empty_string_becomes_a_list() -> None:
    spec = SpecResult.model_validate(SPEC | {"files": "app.py", "out_of_scope": ""})
    assert spec.files == ["app.py"] and spec.out_of_scope == []


def test_multi_line_prose_is_not_split_into_a_list() -> None:
    with pytest.raises(ValidationError, match="list"):
        SpecResult.model_validate(SPEC | {"risks": "The export may be slow.\nAnd large."})


def test_the_task_and_question_lists_are_coerced_too() -> None:
    task = TASK | {"files": "- app.py", "depends_on": "", "acceptance": "- x\n- y"}
    spec = SpecResult.model_validate(SPEC | {"tasks": [task]})
    assert spec.tasks[0].files == ["app.py"] and spec.tasks[0].acceptance == ["x", "y"]
    q = {"question": "q", "why": "w", "options": "- yes\n- no"}
    assert QuestionsResult.model_validate({"summary": "s", "questions": [q]}).questions[
        0
    ].options == ["yes", "no"]


def test_coerced_lists_still_have_their_limits() -> None:
    with pytest.raises(ValidationError):
        SpecResult.model_validate(SPEC | {"risks": "- " + "x" * 401})
    task = TASK | {"acceptance": ""}
    with pytest.raises(ValidationError):
        SpecResult.model_validate(SPEC | {"tasks": [task]})
