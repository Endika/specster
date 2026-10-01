import difflib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import httpx

from specster.schemas import EvidenceRequest

BODY_MAX_BYTES = 64 * 1024
# Past this the rest of a body is not even counted.
COUNT_MAX_BYTES = 16 * 1024 * 1024


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


def normalize(
    content_type: str, raw: bytes, total: int | None = None, more: bool = False
) -> tuple[str, bool]:
    """Sorted, indented JSON when the whole body parses; else the text, cut at BODY_MAX_BYTES.

    `raw` may be only the start of a body of `total` bytes; `more` means reading stopped before
    the end, so `total` is a lower bound.
    """
    size = len(raw) if total is None else total
    whole = size == len(raw) and not more
    if whole and "json" in content_type.lower():
        try:
            pretty = json.dumps(json.loads(raw), indent=2, sort_keys=True, ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
        else:
            raw = (pretty + "\n").encode()
    over = len(raw) > BODY_MAX_BYTES
    cut = over or not whole
    of = f"more than {size:,}" if more else f"{size:,}"
    kept = raw[:BODY_MAX_BYTES]
    try:
        text = kept.decode()
    except UnicodeDecodeError as e:
        # Only a character split by the cut itself is dropped; anything else is binary.
        if not (cut and e.reason == "unexpected end of data"):
            return f"<{of} bytes, binary>", cut
        text = kept[: e.start].decode()
    if over:
        text += f"\n[cut to the first {BODY_MAX_BYTES // 1024} KB of {of} bytes]\n"
    elif cut:
        text += f"\n[cut after {len(raw):,} bytes: the response did not end in time]\n"
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
            # Status and headers only: a body that never ends must not stall the poll.
            with client.stream("GET", url, timeout=2) as response:
                if response.status_code < 500:
                    return None
        except httpx.HTTPError:
            pass
        time.sleep(pause)
    return f"not ready after {timeout_s:g} s"


def capture(
    client: httpx.Client, origin: str, request: EvidenceRequest, deadline_s: float = 30.0
) -> Capture:
    """At most `deadline_s` overall, and at most BODY_MAX_BYTES of the body kept in memory."""
    stop = time.monotonic() + deadline_s
    kept = bytearray()
    total = 0
    more = False
    try:
        with client.stream(
            request.method,
            origin + request.path,
            json=request.body,
            timeout=deadline_s,
            follow_redirects=False,
        ) as response:
            kind = response.headers.get("content-type", "")
            for chunk in response.iter_bytes():
                total += len(chunk)
                kept += chunk[: BODY_MAX_BYTES + 1 - len(kept)]
                if total >= COUNT_MAX_BYTES or time.monotonic() >= stop:
                    more = True
                    break
            status = response.status_code
    except httpx.HTTPError as e:
        return Capture(None, "", "", False, f"{type(e).__name__}: {e}")
    text, cut = normalize(kind, bytes(kept), total, more)
    return Capture(status, kind, text, cut)


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
