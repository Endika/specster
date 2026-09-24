import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

_METRICS_OPEN = "<!-- specster:metrics "
_PLAN_OPEN = "<!-- specster:plan "
_MARKER = re.compile(r"<!-- specster:metrics (\{.*?\}) -->", re.DOTALL)
_PLAN_MARKER = re.compile(r"<!-- specster:plan (\[.*?\]) sha256=([0-9a-f]{64}) -->", re.DOTALL)


class RunMetrics(BaseModel):
    version: int = 1
    run_id: str
    phase: Literal["spec"] = "spec"
    outcome: Literal["questions", "spec", "error", "budget_exhausted"]
    provider: str
    model: str
    input_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    duration_s: float = Field(default=0.0, ge=0)
    turns: int = Field(default=0, ge=0)
    files_read: list[str] = []
    comments_included: int = Field(default=0, ge=0)
    comments_untrusted: int = Field(default=0, ge=0)
    comments_after_label: int = Field(default=0, ge=0)
    comments_edited_after_label: int = Field(default=0, ge=0)
    hidden_removed: int = Field(default=0, ge=0)
    skills_available: list[str] = []
    skills_read: list[str] = []
    skills_inlined: list[str] = []
    plan_max_parallel: int | None = Field(default=None, ge=0)
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


def last_marker(body: str) -> RunMetrics | None:
    """The metrics of a Specster comment: only its last marker, which the renderer writes
    after every text an issue author or the model could have influenced."""
    start = body.rfind(_METRICS_OPEN)
    match = _MARKER.match(body, start) if start != -1 else None
    if match is None:
        return None
    try:
        return RunMetrics.model_validate(json.loads(match.group(1)))
    except (json.JSONDecodeError, ValidationError):
        return None


def last_plan_marker(body: str) -> tuple[list[dict[str, Any]], str] | None:
    """The approved plan of a spec comment (its last plan marker), if its sha256 still matches."""
    start = body.rfind(_PLAN_OPEN)
    match = _PLAN_MARKER.match(body, start) if start != -1 else None
    if match is None:
        return None
    try:
        tasks = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    canonical = json.dumps(tasks, sort_keys=True, separators=(",", ":"))
    digest = match.group(2)
    if not isinstance(tasks, list) or hashlib.sha256(canonical.encode()).hexdigest() != digest:
        return None
    return tasks, digest


def strip_markers(body: str) -> str:
    return _MARKER.sub("", body)


def spent(previous: Sequence[RunMetrics]) -> tuple[float, int]:
    known = sum(m.cost_usd for m in previous if m.cost_usd is not None)
    unknown = sum(1 for m in previous if m.cost_usd is None)
    return round(known, 6), unknown
