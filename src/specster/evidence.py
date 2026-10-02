import difflib
import json
import math
import os
import stat
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from specster.config import PreviewConfig
from specster.sandbox import Server
from specster.schemas import EvidencePage, EvidenceRequest

BODY_MAX_BYTES = 64 * 1024
# Deeper JSON stays text: the parser recurses per level, unbounded without a stack limit.
JSON_MAX_DEPTH = 200
CAPTURE_MAX_S = 30.0
LOG_TAIL_CHARS = 4000
ERROR_MAX_CHARS = 200
# How long a server that broke a request gets to show it exited.
EXIT_GRACE_S = 0.5
OUT_OF_TIME = "the build is out of time"
# Past this the rest of a body is not even counted.
COUNT_MAX_BYTES = 16 * 1024 * 1024
PNG_MAX_BYTES = 5 * 1024 * 1024
PAGE_MAX_HEIGHT = 6000
PAGE_MAX_S = 30.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

Side = Literal["base", "head"]
Viewport = Literal["desktop", "mobile"]
VIEWPORTS: tuple[Viewport, ...] = ("desktop", "mobile")


@dataclass(frozen=True)
class Capture:
    status: int | None
    content_type: str
    text: str
    truncated: bool
    error: str = ""
    # Cut because the response did not end in time, rather than for its size.
    late: bool = False

    @property
    def failure(self) -> str:
        """The error on one line, short enough for a table or a diff."""
        line = " ".join(self.error.split()) or "error"
        return line if len(line) <= ERROR_MAX_CHARS else line[: ERROR_MAX_CHARS - 1] + "\u2026"


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
class Shot:
    viewport: Viewport
    png: bytes | None
    # Why there is no PNG.
    note: str = ""


@dataclass(frozen=True)
class PageItem:
    page: EvidencePage
    base: tuple[Shot, ...]
    head: tuple[Shot, ...]

    @property
    def changed(self) -> bool:
        return any(
            b.png is not None and h.png is not None and b.png != h.png
            for b, h in zip(self.base, self.head, strict=True)
        )


@dataclass(frozen=True)
class EvidenceRun:
    items: tuple[EvidenceItem, ...]
    problems: tuple[SideProblem, ...]
    logs: Mapping[str, str]
    pages: tuple[PageItem, ...] = ()
    browser_logs: Mapping[str, str] = field(default_factory=dict)


# A side's shots by page name, and the browser's log.
Shoot = Callable[[Side], tuple[Mapping[str, Sequence[Shot]], str]]


def _too_deep(raw: bytes) -> bool:
    depth = 0
    in_string = escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # quote
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > JSON_MAX_DEPTH:
                return True
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1
    return False


def normalize(
    content_type: str, raw: bytes, total: int | None = None, more: bool = False
) -> tuple[str, bool]:
    """Sorted, indented JSON when the whole body parses; else the text, cut at BODY_MAX_BYTES.

    `raw` may be only the start of a body of `total` bytes; `more` means reading stopped before
    the end, so `total` is a lower bound.
    """
    size = len(raw) if total is None else total
    whole = size == len(raw) and not more
    if whole and "json" in content_type.lower() and not _too_deep(raw):
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
    return Capture(status, kind, text, cut, late=more and len(kept) <= BODY_MAX_BYTES)


def _side(capture: Capture | None) -> list[str]:
    if capture is None:
        return []
    status = f"none ({capture.failure})" if capture.status is None else str(capture.status)
    return [f"status: {status}\n", *capture.text.splitlines(keepends=True)]


def diff_text(name: str, base: Capture | None, head: Capture | None) -> str:
    """Unified diff base -> head; the status is the first line so a status change shows."""
    lines = difflib.unified_diff(
        _side(base), _side(head), fromfile=f"{name} (base)", tofile=f"{name} (head)"
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _serve(
    client: httpx.Client,
    preview: PreviewConfig,
    requests: Sequence[EvidenceRequest],
    time_left: Callable[[], float],
    server: Server,
    got: list[Capture | None],
) -> str | None:
    """Fills `got` from a started server; why it stopped short, if it did."""
    left = time_left()
    if left <= 0:
        return OUT_OF_TIME
    why = wait_ready(client, preview.ready_url, min(preview.ready_timeout_s, left), server.alive)
    if why is not None:
        return f"{why}: {OUT_OF_TIME}" if left < preview.ready_timeout_s else why
    for i, request in enumerate(requests):
        left = time_left()
        if left <= 0:
            return f"{OUT_OF_TIME}: {i} of {len(requests)} requests made"
        got[i] = answer = capture(client, preview.origin, request, min(CAPTURE_MAX_S, left))
        if answer.status is None and _exited(server):
            return f"the server exited after {i} of {len(requests)} requests"
    return None


def _exited(server: Server) -> bool:
    stop = time.monotonic() + EXIT_GRACE_S
    while server.alive():
        if time.monotonic() >= stop:
            return False
        time.sleep(0.05)
    return True


def _missing(side: Side) -> str:
    return f"not captured: see browser-{side}.log"


def read_shots(
    outdir: Path, pages: Sequence[EvidencePage], side: Side
) -> dict[str, tuple[Shot, ...]]:
    """Each page's `{name}.{viewport}.png` in a folder the sandbox wrote; never through a link."""
    try:
        dir_fd: int | None = os.open(outdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        dir_fd = None
    except OSError as e:
        unreadable = f"unreadable: {e.strerror}"
        return {p.name: tuple(Shot(v, None, unreadable) for v in VIEWPORTS) for p in pages}
    try:
        return {
            p.name: tuple(_shot(dir_fd, f"{p.name}.{v}.png", v, side) for v in VIEWPORTS)
            for p in pages
        }
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _shot(dir_fd: int | None, name: str, viewport: Viewport, side: Side) -> Shot:
    if dir_fd is None:
        return Shot(viewport, None, _missing(side))
    try:
        # Non-blocking, so a FIFO in its place cannot stall the open.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return Shot(viewport, None, _missing(side))
    except OSError as e:
        return Shot(viewport, None, f"unreadable: {e.strerror}")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
        os.close(fd)
        return Shot(viewport, None, "not a regular file")
    with os.fdopen(fd, "rb") as f:
        data = f.read(PNG_MAX_BYTES + 1)
    if len(data) > PNG_MAX_BYTES:
        return Shot(viewport, None, f"over the {PNG_MAX_BYTES // (1024 * 1024)} MB limit")
    if not data.startswith(PNG_MAGIC):
        return Shot(viewport, None, "not a PNG")
    return Shot(viewport, data)


def _shoot(shoot: Shoot, side: Side) -> tuple[Mapping[str, Sequence[Shot]], str]:
    try:
        return shoot(side)
    except Exception as e:
        traceback.print_exc()
        return {}, f"{type(e).__name__}: {e}\n"


def _page_shots(got: Sequence[Shot], note: str) -> tuple[Shot, ...]:
    by_viewport = {s.viewport: s for s in got}
    return tuple(by_viewport.get(v, Shot(v, None, note)) for v in VIEWPORTS)


def collect(
    start_side: Callable[[Side], tuple[Server | None, str | None]],
    preview: PreviewConfig,
    requests: Sequence[EvidenceRequest],
    client: httpx.Client,
    time_left: Callable[[], float] = lambda: math.inf,
    *,
    pages: Sequence[EvidencePage] = (),
    shoot: Shoot | None = None,
) -> EvidenceRun:
    """Each request against the base's server, then the head's; a side that fails says why.

    With `shoot`, each side's pages are screenshot after its requests, with its server still up.
    """
    captures: dict[Side, list[Capture | None]] = {}
    shots: dict[Side, Mapping[str, Sequence[Shot]]] = {}
    notes: dict[Side, str] = {}
    problems: list[SideProblem] = []
    logs: dict[str, str] = {}
    browser_logs: dict[str, str] = {}
    sides: tuple[Side, ...] = ("base", "head")
    for side in sides:
        got: list[Capture | None] = [None] * len(requests)
        why = OUT_OF_TIME if time_left() <= 0 else None
        server = None
        if why is None:
            server, why = start_side(side)
        if server is not None:
            try:
                why = _serve(client, preview, requests, time_left, server, got)
                if why is None and shoot is not None and pages:
                    if time_left() <= 0:
                        why = f"{OUT_OF_TIME}: no screenshots taken"
                        notes[side] = OUT_OF_TIME
                    else:
                        shots[side], browser_logs[side] = _shoot(shoot, side)
            finally:
                logs[side] = server.stop().output
        if why is not None:
            problems.append(SideProblem(side, why, logs.get(side, "")[-LOG_TAIL_CHARS:]))
            notes.setdefault(side, why)
        captures[side] = got
    items = tuple(
        EvidenceItem(r, b, h, diff_text(r.name, b, h))
        for r, b, h in zip(requests, captures["base"], captures["head"], strict=True)
    )

    def side_shots(side: Side, page: EvidencePage) -> tuple[Shot, ...]:
        got = shots.get(side, {}).get(page.name, ())
        return _page_shots(got, notes.get(side, _missing(side)))

    page_items = tuple(
        PageItem(page, side_shots("base", page), side_shots("head", page))
        for page in (pages if shoot is not None else ())
    )
    return EvidenceRun(items, tuple(problems), logs, page_items, browser_logs)


def _body(capture: Capture) -> tuple[str, bytes]:
    ext = "json" if "json" in capture.content_type.lower() else "txt"
    text = capture.text if capture.status is not None else capture.error + "\n"
    return ext, text.encode()


def files(run: EvidenceRun) -> dict[str, bytes]:
    """The evidence branch's files: responses, diffs, screenshots, server and browser logs."""
    out: dict[str, bytes] = {}
    for item in run.items:
        for side, got in (("base", item.base), ("head", item.head)):
            if got is not None:
                ext, data = _body(got)
                out[f"{item.request.name}.{side}.{ext}"] = data
        out[f"{item.request.name}.diff"] = item.diff.encode()
    for side, log in sorted(run.logs.items()):
        out[f"server-{side}.log"] = log.encode()
    for page in run.pages:
        for side, shots in (("base", page.base), ("head", page.head)):
            for shot in shots:
                if shot.png is not None:
                    out[f"{page.page.name}.{side}.{shot.viewport}.png"] = shot.png
    for side, log in sorted(run.browser_logs.items()):
        out[f"browser-{side}.log"] = log.encode()
    return out
