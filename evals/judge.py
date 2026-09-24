import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from specster.llm.base import ChatModel, ToolCall, ToolResult, ToolSpec

CALIBRATION_PATH = Path(__file__).parent / "judge_calibration.yaml"
PASS_SCORE = 4
NUDGE = "Call submit_grade now."

RUBRICS = {
    "spec_is_implementable": (
        "A developer new to the repo could implement it without asking anything."
    ),
    "tasks_are_small_and_ordered": (
        "Each task is a reviewable unit; dependencies are real, not decorative."
    ),
    "questions_change_the_spec": "Every question's answer would change what gets built.",
    "questions_not_answerable_from_repo": (
        "No question asks something the repository already answers."
    ),
}


@dataclass
class Verdict:
    scores: dict[str, int]
    reasons: str

    @property
    def passed(self) -> bool:
        return min(self.scores.values()) >= PASS_SCORE


@dataclass
class CalibrationSample:
    id: str
    label: str
    kind: str
    rubric: list[str]
    thread: str
    output: dict[str, Any]

    def __post_init__(self) -> None:
        if self.label not in ("good", "bad"):
            raise ValueError(f"{self.id}: label must be good or bad, got {self.label}")
        if self.kind not in ("spec", "questions"):
            raise ValueError(f"{self.id}: kind must be spec or questions, got {self.kind}")
        unknown = [r for r in self.rubric if r not in RUBRICS]
        if unknown:
            raise ValueError(f"{self.id}: unknown rubric {unknown}")


def load_calibration(path: Path = CALIBRATION_PATH) -> list[CalibrationSample]:
    return [CalibrationSample(**row) for row in yaml.safe_load(path.read_text(encoding="utf-8"))]


def _scores(call: ToolCall, rubric: list[str]) -> dict[str, int]:
    scores = {k: int(call.arguments[k]) for k in rubric}
    out_of_range = {k: v for k, v in scores.items() if not 1 <= v <= 5}
    if out_of_range:
        raise ValueError(f"scores must be 1-5, got {out_of_range}")
    return scores


def judge(model: ChatModel, rubric: list[str], thread: str, output_json: str) -> Verdict:
    props: dict[str, Any] = {k: {"type": "integer", "minimum": 1, "maximum": 5} for k in rubric}
    props["reasons"] = {"type": "string"}
    tool = ToolSpec(
        "submit_grade",
        "Submit your grades.",
        {
            "type": "object",
            "properties": props,
            "required": [*rubric, "reasons"],
            "additionalProperties": False,
        },
    )
    criteria = "\n".join(f"- {k}: {RUBRICS[k]}" for k in rubric)
    session = model.start(
        "You grade the output of a spec-writing agent. Be strict: 5 means you would ship it as "
        "is. Call submit_grade exactly once.",
        f"Criteria (1-5 each):\n{criteria}",
        f"Issue thread:\n{thread}\n\nAgent output (JSON):\n{output_json}",
        [tool],
    )
    turn = session.send()
    for attempt in range(2):
        errors: list[ToolResult] = []
        for call in turn.tool_calls:
            if call.name != "submit_grade" or call.raw_arguments is not None:
                errors.append(ToolResult(call.id, "Call submit_grade with valid JSON.", True))
                continue
            try:
                return Verdict(_scores(call, rubric), str(call.arguments.get("reasons", "")))
            except (KeyError, TypeError, ValueError) as e:
                errors.append(ToolResult(call.id, f"Invalid grade: {e}", True))
        if attempt == 0:
            turn = session.send(errors, NUDGE)
    raise RuntimeError("judge did not grade: " + json.dumps(turn.text)[:200])
