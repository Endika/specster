import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from specster.config import PreviewConfig
from specster.evidence import (
    BODY_MAX_BYTES,
    JSON_MAX_DEPTH,
    OUT_OF_TIME,
    Capture,
    EvidenceItem,
    EvidenceRun,
    Side,
    capture,
    collect,
    diff_text,
    files,
    normalize,
    wait_ready,
)
from specster.render.evidence import evidence_section
from specster.render.labels import LABELS
from specster.sandbox import Server
from specster.schemas import EvidenceRequest
from tests.test_sandbox import box

LOCAL = pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
ROUTES: dict[str, tuple[str, bytes]] = {
    "/json": ("application/json", b'{"b":1,"a":[2]}'),
    "/text": ("text/plain; charset=utf-8", b"hola"),
    "/bin": ("application/octet-stream", b"\x00\xff"),
    "/badjson": ("application/json", b"{nope"),
    "/big": ("application/json", json.dumps({"x": "y" * 100_000}).encode()),
}
# Endless bodies: (chunk, pause between chunks).
ENDLESS = {"/forever": (b"x" * 4096, 0.005), "/drip": (b"x", 0.1)}
HANGUP = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ENDLESS:
            chunk, pause = ENDLESS[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # No Content-Length on HTTP/1.0: the body runs until the client hangs up.
            while not HANGUP.is_set():
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(pause)
        elif self.path in ROUTES:
            self._send(200, *ROUTES[self.path])
        else:
            self._send(404, "text/plain", b"missing")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._send(200, self.headers.get("Content-Type", ""), body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def app() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    HANGUP.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    HANGUP.set()
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def refused() -> Iterator[str]:
    # Bound but never listening: Linux refuses at once, where a fixed port may just drop the SYN.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_json_is_normalized() -> None:
    text, cut = normalize("application/json; charset=utf-8", b'{"b":1,"a":[2]}')
    assert text == '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n' and not cut


def test_bad_json_and_binary_stay_text() -> None:
    assert normalize("application/json", b"{nope")[0] == "{nope"
    assert normalize("application/octet-stream", b"\x00\xff")[0] == "<2 bytes, binary>"


def test_big_body_is_cut_and_says_so() -> None:
    text, cut = normalize("text/plain", b"x" * (BODY_MAX_BYTES + 10))
    assert cut and text.endswith(f"\n[cut to the first 64 KB of {BODY_MAX_BYTES + 10:,} bytes]\n")


@LOCAL
def test_capture_normalizes_what_the_app_answers(app: str) -> None:
    with httpx.Client() as client:
        got = {
            name: capture(
                client, app, EvidenceRequest(name=name, method="GET", path=f"/{name}", why="w")
            )
            for name in ["json", "text", "bin", "badjson", "big", "nothing"]
        }
    assert got["json"] == Capture(
        200, "application/json", '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n', False
    )
    assert got["text"].text == "hola" and got["bin"].text == "<2 bytes, binary>"
    assert got["badjson"].text == "{nope"
    assert got["big"].truncated and "[cut to the first 64 KB of" in got["big"].text
    assert got["nothing"].status == 404 and got["nothing"].text == "missing"


@LOCAL
def test_capture_sends_method_and_json_body(app: str) -> None:
    req = EvidenceRequest(name="echo", method="POST", path="/echo", body={"x": 1}, why="w")
    with httpx.Client() as client:
        got = capture(client, app, req)
    assert got.status == 200 and '"x": 1' in got.text


@LOCAL
def test_capture_reports_a_refused_connection(refused: str) -> None:
    req = EvidenceRequest(name="a", method="GET", path="/", why="w")
    with httpx.Client() as client:
        got = capture(client, refused, req)
    assert got.status is None and got.error.startswith("ConnectError")


@LOCAL
def test_wait_ready_returns_once_the_app_answers(app: str) -> None:
    with httpx.Client() as client:
        assert wait_ready(client, f"{app}/nothing", 10, alive=lambda: True) is None


@LOCAL
def test_wait_ready_gives_up_when_the_server_dies(refused: str) -> None:
    with httpx.Client() as client:
        why = wait_ready(client, f"{refused}/", 30, alive=lambda: False)
    assert why == "the server exited before it was ready"


@LOCAL
def test_wait_ready_times_out(refused: str) -> None:
    now = iter([0.0, 0.0, 5.0, 11.0])
    with httpx.Client() as client:
        why = wait_ready(
            client, f"{refused}/", 10, alive=lambda: True, clock=lambda: next(now), pause=0
        )
    assert why == "not ready after 10 s"


def test_diff_of_a_missing_side() -> None:
    head = Capture(200, "application/json", '{\n  "a": 1\n}\n', False)
    assert '+  "a": 1' in diff_text("a", None, head)


def test_a_status_change_alone_shows_in_the_diff() -> None:
    base = Capture(200, "text/plain", "same\n", False)
    head = Capture(404, "text/plain", "same\n", False)
    diff = diff_text("a", base, head)
    assert "-status: 200" in diff and "+status: 404" in diff
    assert diff_text("a", base, base) == ""
    assert "+status: none (boom)" in diff_text("a", base, Capture(None, "", "", False, "boom"))


def test_a_failed_capture_puts_its_error_on_one_short_line_of_the_diff() -> None:
    error = "ReadError: " + "line\n" * 100
    diff = diff_text("a", None, Capture(None, "", "", False, error))
    (status,) = [line for line in diff.splitlines() if line.startswith("+status")]
    assert status.startswith("+status: none (ReadError: line line ") and len(status) < 230


def test_deeply_nested_json_falls_back_to_the_text() -> None:
    raw = b"[" * 100_000 + b"]" * 100_000
    text, cut = normalize("application/json", raw)
    assert cut and text.startswith("[[[") and f"of {len(raw):,} bytes]" in text


def test_deep_json_never_reaches_the_parser_even_with_an_unlimited_stack() -> None:
    # With no stack limit, as on GitHub's runners, Python 3.14 recurses through any depth and
    # takes gigabytes before the kernel kills the whole run.
    code = (
        "import resource\n"
        "inf = resource.RLIM_INFINITY\n"
        "resource.setrlimit(resource.RLIMIT_STACK, (inf, inf))\n"
        "from specster.evidence import normalize\n"
        "normalize('application/json', b'[' * 100_000 + b']' * 100_000)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], timeout=30, check=False)
    assert done.returncode == 0


def test_json_at_the_depth_limit_is_still_indented() -> None:
    raw = b"[" * JSON_MAX_DEPTH + b"]" * JSON_MAX_DEPTH
    assert normalize("application/json", raw)[0].startswith("[\n  [")
    deeper = b"[" * (JSON_MAX_DEPTH + 1) + b"]" * (JSON_MAX_DEPTH + 1)
    assert normalize("application/json", deeper)[0] == deeper.decode()


def test_brackets_inside_strings_do_not_count_as_depth() -> None:
    raw = b'{"a": "' + b"[" * (JSON_MAX_DEPTH + 5) + b'\\"["}'
    assert normalize("application/json", raw)[0].startswith('{\n  "a": ')


def test_the_json_check_ignores_case() -> None:
    assert normalize("Application/JSON", b'{"b":1,"a":2}')[0] == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_a_body_that_stopped_early_says_how_much_came() -> None:
    text, cut = normalize("application/json", b'{"a": 1}', total=8, more=True)
    assert cut and text == '{"a": 1}\n[cut after 8 bytes: the response did not end in time]\n'
    text, cut = normalize("text/plain", b"x" * (BODY_MAX_BYTES + 1), total=10**8, more=True)
    assert cut and text.endswith("[cut to the first 64 KB of more than 100,000,000 bytes]\n")


@LOCAL
@pytest.mark.parametrize(("path", "late"), [("/forever", False), ("/drip", True)])
def test_capture_of_an_endless_body_stops_at_the_deadline(app: str, path: str, late: bool) -> None:
    req = EvidenceRequest(name="a", method="GET", path=path, why="w")
    started = time.monotonic()
    with httpx.Client() as client:
        got = capture(client, app, req, deadline_s=1)
    assert time.monotonic() - started < 5
    assert got.status == 200 and got.truncated and "[cut " in got.text and got.late is late
    assert len(got.text.encode()) < BODY_MAX_BYTES + 200


@LOCAL
def test_wait_ready_never_reads_an_endless_body(app: str) -> None:
    started = time.monotonic()
    with httpx.Client() as client:
        assert wait_ready(client, f"{app}/forever", 10, alive=lambda: True) is None
    assert time.monotonic() - started < 2


TWO = [
    EvidenceRequest(name="root", method="GET", path="/", why="w"),
    EvidenceRequest(name="gone", method="GET", path="/gone", why="w"),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


class Countdown:
    """Plenty of time for the first `calls` checks, none after."""

    def __init__(self, calls: int) -> None:
        self.calls = calls

    def __call__(self) -> float:
        self.calls -= 1
        return 3600.0 if self.calls >= 0 else 0.0


def test_collect_out_of_time_starts_no_side() -> None:
    started: list[Side] = []

    def start_side(side: Side) -> tuple[Server | None, str | None]:
        started.append(side)
        return None, "unreachable"

    preview = PreviewConfig(serve_command=["x"], ready_url="http://127.0.0.1:1/")
    with httpx.Client() as client:
        run = collect(start_side, preview, TWO, client, lambda: 0.0)
    assert started == [] and [(p.side, p.reason) for p in run.problems] == [
        ("base", OUT_OF_TIME),
        ("head", OUT_OF_TIME),
    ]
    assert all(i.base is None and i.head is None and not i.changed for i in run.items)


@LOCAL
def test_collect_keeps_what_it_got_before_the_time_ran_out(tmp_path: Path) -> None:
    port = free_port()
    sb = box(tmp_path)
    home = sb.new_home(tmp_path, "home")
    serve = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]

    def start_side(side: Side) -> tuple[Server | None, str | None]:
        return sb.start(serve, tmp_path, home, f"serve {side}"), None

    preview = PreviewConfig(serve_command=serve, ready_url=f"http://127.0.0.1:{port}/")
    # collect, the ready wait and the first request see time left; the second request does not.
    with httpx.Client() as client:
        run = collect(start_side, preview, TWO, client, Countdown(3))
    assert [(p.side, p.reason) for p in run.problems] == [
        ("base", f"{OUT_OF_TIME}: 1 of 2 requests made"),
        ("head", OUT_OF_TIME),
    ]
    root, gone = run.items
    assert root.base is not None and root.base.status == 200 and root.head is None
    assert gone.base is None and set(run.logs) == {"base"}


# Answers one request, then dies the way a crashing app does.
ONE_SHOT = """\
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
server = HTTPServer(("127.0.0.1", int(sys.argv[1])), SimpleHTTPRequestHandler)
server.handle_request()
server.handle_request()
sys.exit("crashed: out of memory")
"""


@LOCAL
def test_a_server_that_exits_mid_run_stops_the_side_with_its_log(tmp_path: Path) -> None:
    port = free_port()
    sb = box(tmp_path)
    home = sb.new_home(tmp_path, "home")
    (tmp_path / "one_shot.py").write_text(ONE_SHOT)
    serve = [sys.executable, "one_shot.py", str(port)]

    def start_side(side: Side) -> tuple[Server | None, str | None]:
        return sb.start(serve, tmp_path, home, f"serve {side}"), None

    preview = PreviewConfig(serve_command=serve, ready_url=f"http://127.0.0.1:{port}/")
    with httpx.Client() as client:
        run = collect(start_side, preview, TWO, client)
    assert [(p.side, p.reason) for p in run.problems] == [
        ("base", "the server exited after 1 of 2 requests"),
        ("head", "the server exited after 1 of 2 requests"),
    ]
    assert all("crashed: out of memory" in p.log_tail for p in run.problems)
    root, gone = run.items
    assert root.base is not None and root.base.status == 200
    assert gone.base is not None and gone.base.status is None and gone.base.error
    body = "\n".join(evidence_section(run, LABELS["en"], None, None, []))
    assert f"`gone` base request failed: `{gone.base.error}`" in body
    assert "crashed: out of memory" in body


def test_files_name_each_side_by_its_content_type_and_skip_a_missing_side() -> None:
    def item(name: str, base: Capture | None, head: Capture | None) -> EvidenceItem:
        request = EvidenceRequest(name=name, method="GET", path="/", why="w")
        return EvidenceItem(request, base, head, diff_text(name, base, head))

    users = item(
        "users",
        Capture(404, "text/plain", "missing", False),
        Capture(200, "Application/JSON; charset=utf-8", "{}\n", False),
    )
    lone = item("lone", None, Capture(None, "", "", False, "ReadTimeout: timed out"))
    out = files(EvidenceRun((users, lone), (), {"head": "listening\n"}))
    assert sorted(out) == [
        "lone.diff",
        "lone.head.txt",
        "server-head.log",
        "users.base.txt",
        "users.diff",
        "users.head.json",
    ]
    assert out["users.head.json"] == b"{}\n" and out["users.base.txt"] == b"missing"
    assert out["lone.head.txt"] == b"ReadTimeout: timed out\n"
    assert out["users.diff"] == users.diff.encode() and out["server-head.log"] == b"listening\n"
