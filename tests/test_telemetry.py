import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue

from specster.telemetry import configure
from tests.test_sandbox import box, home

LOCAL = pytest.mark.block_network(allowed_hosts=["127.0.0.1"])


@dataclass
class Received:
    posts: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)


@pytest.fixture
def collector() -> Iterator[tuple[str, Received]]:
    got = Received()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.posts.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", got
    server.shutdown()
    server.server_close()


def attrs(pairs: list[KeyValue]) -> dict[str, str]:
    return {kv.key: kv.value.string_value for kv in pairs}


def test_without_an_endpoint_nothing_is_configured() -> None:
    assert configure({}, "1.2.3", "42-1") is None
    off = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1", "OTEL_SDK_DISABLED": "true"}
    assert configure(off, "1.2.3", "42-1") is None


@LOCAL
def test_a_run_pushes_metrics_and_traces_to_the_endpoint(
    collector: tuple[str, Received], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, got = collector
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": url, "OTEL_EXPORTER_OTLP_HEADERS": "x-key=abc"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    t = configure(env, "1.2.3", "42-1")
    assert t is not None and t.tracer_provider is not None and t.meter_provider is not None
    with t.tracer_provider.get_tracer("specster").start_as_current_span("run"):
        pass
    t.meter_provider.get_meter("specster").create_gauge("specster.test").set(7)
    t.shutdown()

    assert [path for path, *_ in got.posts].count("/v1/metrics") == 1
    by_path = {path: (headers, body) for path, headers, body in got.posts}
    assert set(by_path) == {"/v1/traces", "/v1/metrics"}
    assert all(h["x-key"] == "abc" for h, _ in by_path.values())
    traces = ExportTraceServiceRequest.FromString(by_path["/v1/traces"][1])
    metrics = ExportMetricsServiceRequest.FromString(by_path["/v1/metrics"][1])
    expected = {
        "service.name": "specster",
        "service.version": "1.2.3",
        "service.instance.id": "42-1",
    }
    for resource in (
        traces.resource_spans[0].resource,
        metrics.resource_metrics[0].resource,
    ):
        assert expected.items() <= attrs(list(resource.attributes)).items()
    assert traces.resource_spans[0].scope_spans[0].spans[0].name == "run"
    assert metrics.resource_metrics[0].scope_metrics[0].metrics[0].name == "specster.test"


@LOCAL
def test_a_metrics_endpoint_alone_sends_no_traces(
    collector: tuple[str, Received], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, got = collector
    env = {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": f"{url}/v1/metrics"}
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"]
    )
    t = configure(env, "1.2.3", "42-1")
    assert t is not None and t.meter_provider is not None
    assert t.tracer_provider is None
    t.meter_provider.get_meter("specster").create_gauge("specster.test").set(7)
    t.shutdown()

    assert [path for path, *_ in got.posts] == ["/v1/metrics"]


@LOCAL
def test_a_traces_endpoint_alone_sends_no_metrics(
    collector: tuple[str, Received], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, got = collector
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{url}/v1/traces"}
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
    )
    t = configure(env, "1.2.3", "42-1")
    assert t is not None and t.tracer_provider is not None
    assert t.meter_provider is None
    with t.tracer_provider.get_tracer("specster").start_as_current_span("run"):
        pass
    t.shutdown()

    assert [path for path, *_ in got.posts] == ["/v1/traces"]


class SilentServer:
    """Accepts connections and never answers, like an endpoint that hangs."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.05)
        self.port: int = self.sock.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                self._held.append(self.sock.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    def close(self) -> None:
        # Stop accepting first, so no connection lands after the held ones are closed.
        self._stop.set()
        self._thread.join()
        self.sock.close()
        for c in self._held:
            c.close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


@LOCAL
@pytest.mark.parametrize("kind", ["closed", "silent"])
def test_a_dead_endpoint_never_raises_and_flush_is_bounded(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv = SilentServer()
    port = closed_port() if kind == "closed" else srv.port
    try:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}"}
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", env["OTEL_EXPORTER_OTLP_ENDPOINT"])
        t = configure(env, "1.2.3", "42-1")
        assert t is not None and t.tracer_provider is not None and t.meter_provider is not None
        tracer = t.tracer_provider.get_tracer("specster")
        for _ in range(1500):
            with tracer.start_as_current_span("call"):
                pass
        t.meter_provider.get_meter("specster").create_gauge("specster.test").set(7)
        started = time.monotonic()
        t.shutdown()
        took = time.monotonic() - started
        print(f"flush with a {kind} endpoint took {took:.2f}s")
        assert took < 15
    finally:
        srv.close()


def test_the_sandbox_sees_no_otel_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-key=abc")
    res = box(tmp_path).run(["env"], tmp_path, home(tmp_path), "tests")
    assert res.ok
    assert not [line for line in res.output.splitlines() if line.startswith("OTEL_")]
