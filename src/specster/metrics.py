import json
import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ValidationError

_MARKER = re.compile(r"<!-- specster:metrics (\{.*?\}) -->", re.DOTALL)


class RunMetrics(BaseModel):
    version: int = 1
    run_id: str
    phase: Literal["spec"] = "spec"
    outcome: Literal["questions", "spec", "error", "budget_exhausted"]
    provider: str
    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_s: float = 0.0
    turns: int = 0
    files_read: list[str] = []
    comments_included: int = 0
    comments_untrusted: int = 0
    comments_after_label: int = 0
    comments_edited_after_label: int = 0
    hidden_removed: int = 0
    skills_available: list[str] = []
    skills_read: list[str] = []
    plan_max_parallel: int | None = None
    truncations: list[str] = []
    warnings: list[str] = []


def encode_marker(m: RunMetrics) -> str:
    # Escaping < and > keeps valid JSON while making "-->" impossible inside the comment.
    payload = m.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- specster:metrics {payload} -->"


def extract_markers(body: str) -> list[RunMetrics]:
    found = []
    for match in _MARKER.finditer(body):
        try:
            found.append(RunMetrics.model_validate(json.loads(match.group(1))))
        except (json.JSONDecodeError, ValidationError):
            continue
    return found


def strip_markers(body: str) -> str:
    return _MARKER.sub("", body)


def spent(previous: Sequence[RunMetrics]) -> tuple[float, int]:
    known = sum(m.cost_usd for m in previous if m.cost_usd is not None)
    unknown = sum(1 for m in previous if m.cost_usd is None)
    return round(known, 6), unknown
