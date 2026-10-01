"""OTLP export of a run's metrics and trace; off unless an OTLP endpoint is set."""

import contextlib
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
_METRICS_ENDPOINT = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"
_TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
# The SDK defaults to 10 s per export and retries inside it; this keeps a dead endpoint cheap.
_EXPORT_TIMEOUT_S = 5


def signals(environ: Mapping[str, str]) -> tuple[bool, bool]:
    """Whether metrics and traces are on: each needs its own endpoint or the shared one."""
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False, False
    shared = bool(environ.get(_ENDPOINT))
    metrics_on = shared or bool(environ.get(_METRICS_ENDPOINT))
    traces_on = shared or bool(environ.get(_TRACES_ENDPOINT))
    return metrics_on, traces_on


def _left_ms(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


@dataclass
class Telemetry:
    tracer_provider: TracerProvider | None
    meter_provider: MeterProvider | None
    stop_span_exporter: Callable[[], None] | None

    def install(self) -> None:
        # A signal that is off keeps the API's no-op provider.
        if self.tracer_provider is not None:
            trace.set_tracer_provider(self.tracer_provider)
        if self.meter_provider is not None:
            metrics.set_meter_provider(self.meter_provider)

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """Push what is left; never raises, returns within about timeout_s."""
        deadline = time.monotonic() + timeout_s
        if self.tracer_provider is not None:
            self._stop_traces(self.tracer_provider, time.monotonic() + timeout_s * 0.6)
        if self.meter_provider is None:
            return
        try:
            # Its reader collects once more on shutdown; a force_flush first would send every
            # gauge twice, since a cumulative gauge keeps its last value.
            self.meter_provider.shutdown(timeout_millis=_left_ms(deadline))
        except Exception:
            traceback.print_exc()
            print("specster: telemetry export failed", file=sys.stderr)

    def _stop_traces(self, provider: TracerProvider, deadline: float) -> None:
        # The SDK's span flush ignores its timeout and drains the whole queue, so bound it here.
        def drain() -> None:
            try:
                provider.force_flush(_left_ms(deadline))
                provider.shutdown()
            except Exception:
                traceback.print_exc()
                print("specster: telemetry export failed", file=sys.stderr)

        worker = threading.Thread(target=drain, daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            print("specster: telemetry export timed out", file=sys.stderr)
            # Ends the exporter's retry waits so the abandoned thread and the atexit hook finish.
            if self.stop_span_exporter is not None:
                with contextlib.suppress(Exception):
                    self.stop_span_exporter()


def configure(environ: Mapping[str, str], version: str, instance_id: str) -> Telemetry | None:
    # `environ` only gates; the exporters read endpoint and headers from os.environ themselves.
    want_metrics, want_traces = signals(environ)
    if not (want_metrics or want_traces):
        return None
    try:
        resource = Resource.create(
            {
                "service.name": "specster",
                "service.version": version,
                "service.instance.id": instance_id,
            }
        )
        tracer_provider = None
        stop_span_exporter = None
        if want_traces:
            tracer_provider = TracerProvider(resource=resource)
            span_exporter = OTLPSpanExporter(timeout=_EXPORT_TIMEOUT_S)
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
            stop_span_exporter = span_exporter.shutdown
        meter_provider = None
        if want_metrics:
            # One reading at shutdown: a run is one process, so no periodic export is needed.
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(timeout=_EXPORT_TIMEOUT_S), export_interval_millis=3_600_000
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        return Telemetry(tracer_provider, meter_provider, stop_span_exporter)
    except Exception:
        traceback.print_exc()
        return None
