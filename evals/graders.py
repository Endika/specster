import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from specster.agent import AgentOutcome
from specster.schemas import QuestionsResult, SpecResult
from specster.workspace import Workspace

CASES_PATH = Path(__file__).parent / "cases.yaml"
EXPLORE_TOOLS = ("grep", "read_file", "list_dir")

_ES = {"el", "la", "de", "que", "y", "los", "las", "para", "con", "una", "por"}
_EN = {"the", "and", "of", "to", "with", "for", "that", "this", "is", "a"}


@dataclass
class Case:
    id: str
    title: str
    body: str
    expect: str
    must_touch: list[str] = field(default_factory=list)
    max_questions: int = 5
    must_explore: bool = False
    language: str = "en"
    judge: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.expect not in ("spec", "questions"):
            raise ValueError(f"{self.id}: expect must be spec or questions, got {self.expect}")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    return [Case(**row) for row in yaml.safe_load(path.read_text(encoding="utf-8"))]


def _language_ok(text: str, language: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text.lower())
    es, en = sum(w in _ES for w in words), sum(w in _EN for w in words)
    return es > en if language == "es" else en >= es


def grade(case: Case, outcome: AgentOutcome, ws: Workspace, tool_names: list[str]) -> list[Check]:
    r = outcome.result
    kind = "spec" if isinstance(r, SpecResult) else "questions"
    checks = [Check("decision", kind == case.expect, f"got {kind}, expected {case.expect}")]
    text = json.dumps(r.model_dump(exclude={"closing_line"}), ensure_ascii=False)
    checks.append(Check("language", _language_ok(text, case.language), f"expected {case.language}"))
    if isinstance(r, SpecResult):
        touched = {f for t in outcome.tasks for f in t.files} | set(r.files)
        missing = [f for f in case.must_touch if f not in touched]
        checks.append(Check("must_touch", not missing, f"missing {missing}"))
        existing = set(ws.files())
        dirs = {f.rsplit("/", 1)[0] for f in existing if "/" in f}
        invented = sorted(
            f for f in touched if f not in existing and "/" in f and f.rsplit("/", 1)[0] not in dirs
        )
        checks.append(Check("no_invented_dirs", not invented, f"{invented}"))
    if isinstance(r, QuestionsResult):
        n = len(r.questions)
        checks.append(Check("question_count", n <= case.max_questions, f"{n} questions"))
    if case.must_explore:
        explored = any(n in EXPLORE_TOOLS for n in tool_names)
        checks.append(Check("explored_before_submitting", explored, f"tools: {tool_names}"))
    return checks
