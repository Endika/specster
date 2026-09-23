import json

import pytest
from pydantic import ValidationError

from specster.schemas import PlanTask, QuestionsResult, SpecResult, json_schema


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
