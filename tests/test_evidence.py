import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from specster.evidence import BODY_MAX_BYTES, Capture, capture, diff_text, normalize, wait_ready
from specster.schemas import EvidenceRequest

LOCAL = pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
ROUTES: dict[str, tuple[str, bytes]] = {
    "/json": ("application/json", b'{"b":1,"a":[2]}'),
    "/text": ("text/plain; charset=utf-8", b"hola"),
    "/bin": ("application/octet-stream", b"\x00\xff"),
    "/badjson": ("application/json", b"{nope"),
    "/big": ("application/json", json.dumps({"x": "y" * 100_000}).encode()),
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ROUTES:
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
    yield f"http://127.0.0.1:{server.server_address[1]}"
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
    assert "+status: none (error)" in diff_text("a", base, Capture(None, "", "", False, "boom"))
