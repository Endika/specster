from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SUBMIT_QUESTIONS = "submit_questions"
SUBMIT_SPEC = "submit_spec"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(_Out):
    question: str = Field(description="One concrete question the spec depends on.")
    why: str = Field(description="What changes in the spec depending on the answer.")
    options: list[str] = Field(default=[], description="Likely answers, if there are a few.")


class QuestionsResult(_Out):
    summary: str = Field(description="What is already clear, in two or three sentences.")
    questions: list[Question] = Field(min_length=1, max_length=5)
    closing_line: str = Field(default="", description="The single closing sentence.")


class PlanTask(_Out):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Short slug, e.g. add-parser.")
    title: str
    description: str
    files: list[str] = Field(min_length=1, description="Repo-relative paths this task touches.")
    depends_on: list[str] = Field(default=[], description="Ids of tasks that must finish first.")
    acceptance: list[str] = Field(min_length=1, description="Observable, checkable criteria.")


class SpecResult(_Out):
    title: str
    objective: str
    in_scope: list[str]
    out_of_scope: list[str]
    files: list[str] = Field(description="Every repo-relative path the change touches.")
    approach: str
    risks: list[str]
    test_strategy: str
    tasks: list[PlanTask] = Field(min_length=1)
    closing_line: str = Field(default="", description="The single closing sentence.")


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {
            k: _inline(v, defs)
            for k, v in node.items()
            if k != "$defs" and not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    result: dict[str, Any] = _inline(raw, raw.get("$defs", {}))
    return result
