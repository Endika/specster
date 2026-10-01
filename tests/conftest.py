import shutil
import tempfile
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider, _Gauge
from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Imported here under a filter of its own: pytest lets a command-line `-W error` override any
# ini filterwarnings entry, and google-genai trips a Python 3.14 deprecation at import time.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'_UnionGenericAlias' is deprecated",
        category=DeprecationWarning,
        module=r"google\.genai(\.|$)",
    )
    import google.genai.types  # noqa: F401


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A sandbox TMPDIR makes tmp_path longer than the 108 bytes an AF_UNIX path allows."""
    path = Path(tempfile.mkdtemp(prefix="sp-", dir="/tmp"))
    yield path
    shutil.rmtree(path)


@dataclass(frozen=True)
class Point:
    name: str
    unit: str
    value: float
    attributes: dict[str, object]


@pytest.fixture(scope="session")
def metric_reader() -> InMemoryMetricReader:
    # Delta makes a gauge report only what was set since the previous reading.
    reader = InMemoryMetricReader(preferred_temporality={_Gauge: AggregationTemporality.DELTA})
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


@pytest.fixture
def metric_points(metric_reader: InMemoryMetricReader) -> Callable[[], list[Point]]:
    """Reads and clears the gauges set since the previous call, starting empty in each test."""

    def read() -> list[Point]:
        data = metric_reader.get_metrics_data()
        if data is None:
            return []
        return [
            Point(m.name, m.unit or "", p.value, dict(p.attributes or {}))
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if isinstance(m.data, Gauge)
            for p in m.data.data_points
        ]

    read()
    return read


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def spans(span_exporter: InMemorySpanExporter) -> Callable[[], list[ReadableSpan]]:
    """Reads and clears the spans ended since the previous call, starting empty in each test."""

    def read() -> list[ReadableSpan]:
        out = list(span_exporter.get_finished_spans())
        span_exporter.clear()
        return out

    read()
    return read
