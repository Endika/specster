import difflib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import httpx

from specster.schemas import EvidenceRequest

BODY_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class Capture:
    status: int | None
    content_type: str
    text: str
    truncated: bool
    error: str = ""


@dataclass(frozen=True)
class EvidenceItem:
    request: EvidenceRequest
    base: Capture | None
    head: Capture | None
    diff: str

    @property
    def changed(self) -> bool:
        return bool(self.diff)


@dataclass(frozen=True)
class SideProblem:
    side: Literal["base", "head"]
    reason: str
    log_tail: str


@dataclass(frozen=True)
class EvidenceRun:
    items: tuple[EvidenceItem, ...]
    problems: tuple[SideProblem, ...]
    logs: Mapping[str, str]


def normalize(content_type: str, raw: bytes) -> tuple[str, bool]:
    """Sorted, indented JSON when the whole body parses; else the text, cut at BODY_MAX_BYTES."""
    size = len(raw)
    if "json" in content_type:
        try:
            parsed = json.loads(raw)
        except ValueError:
            pass
        else:
            raw = (json.dumps(parsed, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    cut = len(raw) > BODY_MAX_BYTES
    kept = raw[:BODY_MAX_BYTES]
    try:
        text = kept.decode()
    except UnicodeDecodeError as e:
        # Only a character split by the cut itself is dropped; anything else is binary.
        if not (cut and e.reason == "unexpected end of data"):
            return f"<{size} bytes, binary>", False
        text = kept[: e.start].decode()
    if cut:
        text += f"\n[cut to the first {BODY_MAX_BYTES // 1024} KB of {size:,} bytes]\n"
    return text, cut


def wait_ready(
    client: httpx.Client,
    url: str,
    timeout_s: float,
    alive: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    pause: float = 0.25,
) -> str | None:
    """None once `url` answers below 500, else why the server never got ready."""
    start = clock()
    while clock() - start < timeout_s:
        if not alive():
            return "the server exited before it was ready"
        try:
            if client.get(url, timeout=2).status_code < 500:
                return None
        except httpx.HTTPError:
            pass
        time.sleep(pause)
    return f"not ready after {timeout_s:g} s"


def capture(client: httpx.Client, origin: str, request: EvidenceRequest) -> Capture:
    try:
        response = client.request(
            request.method,
            origin + request.path,
            json=request.body,
            timeout=30,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        return Capture(None, "", "", False, f"{type(e).__name__}: {e}")
    kind = response.headers.get("content-type", "")
    text, cut = normalize(kind, response.content)
    return Capture(response.status_code, kind, text, cut)


def _side(capture: Capture | None) -> list[str]:
    if capture is None:
        return []
    status = "none (error)" if capture.status is None else str(capture.status)
    return [f"status: {status}\n", *capture.text.splitlines(keepends=True)]


def diff_text(name: str, base: Capture | None, head: Capture | None) -> str:
    """Unified diff base -> head; the status is the first line so a status change shows."""
    lines = difflib.unified_diff(
        _side(base), _side(head), fromfile=f"{name} (base)", tofile=f"{name} (head)"
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
