from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUBMIT_QUESTIONS = "submit_questions"
SUBMIT_SPEC = "submit_spec"
SUBMIT_TASK = "submit_task"
SUBMIT_REVIEW = "submit_review"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Hard caps are about twice the lengths the prompt asks for, so a slightly long answer still
# validates while a runaway one is sent back to the model.
def _text(max_chars: int, description: str = "") -> Any:
    return Field(max_length=max_chars, description=description or None)


Item = Annotated[str, Field(max_length=400)]
_MARKDOWN = 'Markdown with real line breaks and quotes, not JSON escapes such as \\n or \\".'


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


# 40 keeps "fix(<id>): address review findings" within a 72-character commit subject.
TASK_ID_MAX = 40
# A soft cap: a task over it still validates, but its worker reads and edits every file in the
# turns of one task.
TASK_FILES_MAX = 5


class PlanTask(_Out):
    id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        max_length=TASK_ID_MAX,
        description="Short slug, e.g. add-parser.",
    )
    title: str = _text(120)
    description: str = _text(1500, "What to change, concretely.")
    files: list[str] = Field(min_length=1, description="Repo-relative paths this task touches.")
    depends_on: list[str] = Field(default=[], description="Ids of tasks that must finish first.")
    acceptance: list[Annotated[str, Field(max_length=300)]] = Field(
        min_length=1, description="Observable, checkable criteria."
    )


EVIDENCE_MAX = 10


class EvidenceRequest(_Out):
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=40, description="Short slug, e.g. list-users."
    )
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(
        pattern=r"^/([^\s#/][^\s#]*)?$",
        max_length=300,
        description="Path and query on the app, e.g. /api/users?limit=2; never a host.",
    )
    body: dict[str, Any] | list[Any] | None = Field(default=None, description="JSON body, if any.")
    why: str = _text(200, "What this request shows about the change, one sentence.")


class EvidencePage(_Out):
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=40, description="Short slug, e.g. users-page."
    )
    path: str = Field(
        pattern=r"^/([^\s#/][^\s#]*)?$",
        max_length=300,
        description="Path and query of a page on the app, e.g. /users; never a host.",
    )
    why: str = _text(200, "What this page shows about the change, one sentence.")


class SpecResult(_Out):
    title: str = _text(120)
    objective: str = _text(1000)
    in_scope: list[Item]
    out_of_scope: list[Item]
    files: list[str] = Field(description="Every repo-relative path the change touches.")
    approach: str = _text(4000, _MARKDOWN)
    risks: list[Item]
    test_strategy: str = _text(2000, _MARKDOWN)
    tasks: list[PlanTask] = Field(min_length=1)
    evidence: list[EvidenceRequest] = Field(
        default=[],
        max_length=EVIDENCE_MAX,
        description="HTTP requests run against the app before and after the change.",
    )
    pages: list[EvidencePage] = Field(
        default=[],
        max_length=EVIDENCE_MAX,
        description="Pages screenshotted before and after the change.",
    )
    changes: list[Item] = Field(
        default=[],
        max_length=20,
        description="Only in revision mode: each change from the previous spec, one line each.",
    )
    closing_line: str = Field(default="", max_length=300, description="The closing sentence.")

    @model_validator(mode="after")
    def _evidence_total(self) -> Self:
        if len(self.evidence) + len(self.pages) > EVIDENCE_MAX:
            raise ValueError(f"evidence and pages together are at most {EVIDENCE_MAX}")
        return self


class TaskSubmission(_Out):
    summary: str = _text(1500, "What you changed and how you checked it, in a few sentences.")
    commit_subject: str = _text(200, "One-line Conventional Commit subject, at most 72 chars.")


class Finding(_Out):
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="The plan task at fault.")
    file: str = Field(
        min_length=1,
        max_length=300,
        pattern=r"^[^\s#`]+$",
        description="Repo-relative path the finding is about.",
    )
    severity: Literal["critical", "important", "minor"]
    description: str = _text(800, "What is wrong and what to change, concretely.")


class ReviewResult(_Out):
    verdict: Literal["approve", "changes"]
    findings: list[Finding] = Field(default=[], max_length=30)


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
