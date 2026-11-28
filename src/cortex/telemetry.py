"""OpenTelemetry traces, metrics and logs over OTLP/HTTP, on only when OTEL_EXPORTER_OTLP_ENDPOINT is set.

Off, the SDK and exporters are never imported and every function here returns at once, so tests
and local runs need nothing and pay nothing. (The mcp library already loads the OpenTelemetry API,
which does nothing without a provider; Cortex never sets a global one.) On, the standard OTEL_* variables configure the exporters
(endpoint, headers, OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES).

Spans and metrics carry names, counts, durations, flags and status codes; never query text, note
paths or content, token names or keys. Exported logs are Cortex's own log lines, exactly what
stdout gets, so they can name a note path (a failed embed, an outside edit), never its content.
"""

from __future__ import annotations

import contextlib
import logging
import os
import warnings
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger("cortex.telemetry")

_tracer: Any = None
_meter: Any = None
_instruments: dict[str, Any] = {}
_providers: list[Any] = []
_log_handler: logging.Handler | None = None


def enabled() -> bool:
    return _tracer is not None


def setup(version: str, exporters: tuple[Any, Any, Any] | None = None) -> bool:
    """Start exporting if an OTLP endpoint is configured. Returns whether it did.

    `exporters` (span exporter, metric reader, log exporter) replaces the OTLP ones; tests pass
    in-memory ones. The providers stay Cortex's own rather than global, so nothing else in the
    process starts exporting through them.
    """
    global _tracer, _meter, _log_handler
    if _tracer is not None or not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return enabled()
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if exporters is None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporters = (OTLPSpanExporter(), PeriodicExportingMetricReader(OTLPMetricExporter()), OTLPLogExporter())
    span_exporter, metric_reader, log_exporter = exporters

    # OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES are merged in by Resource.create and win.
    resource = Resource.create({"service.name": "cortex", "service.version": version})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    _providers[:] = [tracer_provider, meter_provider, logger_provider]

    # Cortex's own log lines (the same ones stdout gets), not every library's debug chatter.
    # The SDK's handler is deprecated in favour of an instrumentation package; it still works,
    # and the lock file pins it, so the extra dependency isn't worth it yet.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _log_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logging.getLogger("cortex").addHandler(_log_handler)

    meter = _meter = meter_provider.get_meter("cortex")
    _instruments.update(
        tool_calls=meter.create_counter("cortex.tool.calls", description="MCP tool calls by tool and outcome"),
        tool_duration=meter.create_histogram("cortex.tool.duration", unit="s", description="MCP tool call time"),
        searches=meter.create_counter("cortex.search.count", description="Searches by mode and fallback"),
        search_duration=meter.create_histogram(
            "cortex.search.duration",
            unit="s",
            description="Search time per stage: embed, keyword, vector, rerank, total",
        ),
        voyage_calls=meter.create_counter("cortex.voyage.requests", description="Voyage HTTP requests by outcome"),
        voyage_duration=meter.create_histogram("cortex.voyage.duration", unit="s", description="Voyage request time"),
        voyage_items=meter.create_histogram(
            "cortex.voyage.batch_size", unit="{item}", description="Texts or documents per Voyage request"
        ),
        backfilled=meter.create_counter("cortex.backfill.chunks", unit="{chunk}", description="Chunks given vectors"),
        backfill_failures=meter.create_counter("cortex.backfill.failures", description="Backfill passes that failed"),
        synced=meter.create_counter("cortex.sync.changed", unit="{note}", description="Notes changed outside Cortex"),
        sync_duration=meter.create_histogram(
            "cortex.sync.duration", unit="s", description="Sync with the vault folder"
        ),
    )
    _tracer = tracer_provider.get_tracer("cortex")
    log.info("telemetry on: exporting traces, metrics and logs over OTLP/HTTP")
    return True


def shutdown() -> None:
    """Flush what is buffered; called once when the server stops."""
    global _tracer, _meter, _log_handler
    if _log_handler is not None:
        logging.getLogger("cortex").removeHandler(_log_handler)
        _log_handler = None
    for provider in _providers:
        with contextlib.suppress(Exception):
            provider.shutdown()
    _providers.clear()
    _instruments.clear()
    _tracer = _meter = None


class _NoSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        pass


@contextlib.contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """A span around the block; an exception leaving it marks the span failed and propagates."""
    if _tracer is None:
        yield _NoSpan()
        return
    with _tracer.start_as_current_span(name, attributes=attributes) as current:
        yield current


def _add(name: str, value: float, attributes: dict[str, Any]) -> None:
    instrument = _instruments.get(name)
    if instrument is not None:
        instrument.add(value, attributes)


def _record(name: str, value: float, attributes: dict[str, Any]) -> None:
    instrument = _instruments.get(name)
    if instrument is not None:
        instrument.record(value, attributes)


def tool_call(tool: str, seconds: float, outcome: str) -> None:
    """outcome: ok, refused (a rule said no) or error (a bug)."""
    attributes = {"tool": tool, "outcome": outcome}
    _add("tool_calls", 1, attributes)
    _record("tool_duration", seconds, attributes)


def search_done(stages: dict[str, float], *, semantic: bool, reranked: bool, exact: bool) -> None:
    _add("searches", 1, {"semantic": semantic, "reranked": reranked, "exact": exact})
    for stage, seconds in stages.items():
        _record("search_duration", seconds, {"stage": stage})


def voyage_call(operation: str, items: int, seconds: float, outcome: str) -> None:
    """outcome: ok, an HTTP status code such as 429, timeout or unreachable."""
    attributes = {"operation": operation, "outcome": outcome}
    _add("voyage_calls", 1, attributes)
    _record("voyage_duration", seconds, attributes)
    _record("voyage_items", items, {"operation": operation})


def backfilled(chunks: int, failed: bool) -> None:
    _add("backfilled", chunks, {})
    if failed:
        _add("backfill_failures", 1, {})


def synced(changed: int, seconds: float) -> None:
    _add("synced", changed, {})
    _record("sync_duration", seconds, {})


def watch_pending(count: Callable[[], int]) -> None:
    """Report how many notes wait for vectors each time metrics are exported."""
    if _meter is None:
        return
    from opentelemetry.metrics import Observation

    _meter.create_observable_gauge(
        "cortex.pending.notes",
        callbacks=[lambda options: [Observation(count())]],
        unit="{note}",
        description="Notes saved but still waiting for vectors",
    )
