from pathlib import Path

import pytest

from specster.config import PersonaConfig, SkillsConfig
from specster.llm.base import ToolCall
from specster.prompts import reviewer_system_prompt
from specster.review import blocking, run_review
from specster.skills import load_skills
from specster.workspace import Workspace
from tests.fakes import ScriptedModel


def finding(sev: str, task: str = "a") -> dict[str, str]:
    return {"task_id": task, "file": "app.py", "severity": sev, "description": "d"}


def review(tmp_path: Path, script: list[list[ToolCall] | str]) -> tuple[object, ScriptedModel]:
    (tmp_path / "app.py").write_text("x = 1\n")
    skills = load_skills(tmp_path, SkillsConfig(), "review", lambda *_: b"", None)
    model = ScriptedModel(script)
    out = run_review(model, "s", "c", "u", Workspace(tmp_path), skills, {"a", "b"}, 5)
    return out, model


def submit(verdict: str, *findings: dict[str, str]) -> list[ToolCall]:
    return [ToolCall("r", "submit_review", {"verdict": verdict, "findings": list(findings)})]


def test_an_unknown_task_id_is_sent_back(tmp_path: Path) -> None:
    out, model = review(
        tmp_path,
        [submit("changes", finding("critical", "zz")), submit("changes", finding("important"))],
    )
    assert "unknown task_id zz; use one of: a, b" in model.received[1][0].content
    assert out.result.verdict == "changes"  # type: ignore[attr-defined]
    assert [f.severity for f in blocking(out.result)] == ["important"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("verdict", "severity", "message"),
    [
        ("approve", "critical", "approve cannot carry"),
        ("changes", "minor", "changes needs at least one"),
    ],
)
def test_a_verdict_that_contradicts_its_findings_is_sent_back(
    tmp_path: Path, verdict: str, severity: str, message: str
) -> None:
    _, model = review(tmp_path, [submit(verdict, finding(severity)), submit("approve")])
    assert message in model.received[1][0].content


def test_minor_findings_ride_along_with_an_approval(tmp_path: Path) -> None:
    out, _ = review(tmp_path, [submit("approve", finding("minor"))])
    assert out.result.verdict == "approve" and blocking(out.result) == []  # type: ignore[attr-defined]


def test_the_prompt_says_plainly_when_no_tests_ran() -> None:
    assert "No tests were run" in reviewer_system_prompt(PersonaConfig(), [], has_tests=False)
    assert "No tests were run" not in reviewer_system_prompt(PersonaConfig(), [], has_tests=True)


@pytest.mark.parametrize("path", ["a b.py", "fixes#3", "x`.py", "", "a" * 301])
def test_a_finding_file_that_is_not_a_repo_path_is_sent_back(tmp_path: Path, path: str) -> None:
    bad = {"task_id": "a", "file": path, "severity": "important", "description": "d"}
    out, model = review(tmp_path, [submit("changes", bad), submit("changes", finding("important"))])
    assert "Invalid submission" in model.received[1][0].content
    assert out.result.findings[0].file == "app.py"  # type: ignore[attr-defined]
