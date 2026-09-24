from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

SUBMIT_QUESTIONS = "submit_questions"
SUBMIT_SPEC = "submit_spec"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Hard caps are about twice the lengths the prompt asks for, so a slightly long answer still
# validates while a runaway one is sent back to the model.
def _text(max_chars: int, description: str = "") -> Any:
    return Field(max_length=max_chars, description=description or None)


Item = Annotated[str, Field(max_length=400)]


class Question(_Out):
    question: str = _text(400, "One concrete question the spec depends on, one sentence.")
    why: str = _text(400, "What changes in the spec depending on the answer, one sentence.")
    options: list[Annotated[str, Field(max_length=120)]] = Field(
        default=[], max_length=6, description="Likely answers, if there are a few."
    )


class QuestionsResult(_Out):
    summary: str = _text(800, "What is already clear, in one or two sentences.")
    questions: list[Question] = Field(min_length=1, max_length=5)
    closing_line: str = Field(default="", max_length=300, description="The closing sentence.")


class PlanTask(_Out):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Short slug, e.g. add-parser.")
    title: str = _text(120)
    description: str = _text(1500, "What to change, concretely.")
    files: list[str] = Field(min_length=1, description="Repo-relative paths this task touches.")
    depends_on: list[str] = Field(default=[], description="Ids of tasks that must finish first.")
    acceptance: list[Annotated[str, Field(max_length=300)]] = Field(
        min_length=1, description="Observable, checkable criteria."
    )


class SpecResult(_Out):
    title: str = _text(120)
    objective: str = _text(1000)
    in_scope: list[Item]
    out_of_scope: list[Item]
    files: list[str] = Field(description="Every repo-relative path the change touches.")
    approach: str = _text(4000)
    risks: list[Item]
    test_strategy: str = _text(2000)
    tasks: list[PlanTask] = Field(min_length=1)
    closing_line: str = Field(default="", max_length=300, description="The closing sentence.")


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
