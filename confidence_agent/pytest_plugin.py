"""pytest plugin loaded by the confidence agent during each test run.

It installs OpenTelemetry SDK tracer and meter providers that keep spans and
metrics in memory, captures WARNING+ log records, measures each test's peak
memory with tracemalloc, tags everything with the test that produced it, and
writes the result to the JSON file named by CONFIDENCE_TELEMETRY_OUT.

The code under test only needs the OpenTelemetry API; nothing in the target
repository has to change.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
import tracemalloc
from typing import Any, Dict, List, Optional

_current_test: Optional[str] = None
_logs: List[Dict[str, Any]] = []
_exporter = None
_metric_reader = None
_metrics: List[Dict[str, Any]] = []
_memory: Dict[str, int] = {}  # test id -> peak bytes allocated during the test
_test_ids: List[str] = []  # pytest node ids, the ids metrics and memory are keyed by
_track_memory = os.environ.get("CONFIDENCE_MEMORY", "1") != "0"


class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        exc_type = None
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0].__name__
        _logs.append(
            {
                "level": record.levelname,
                "logger": record.name,
                # The unformatted template groups repeats of the same message.
                "template": str(record.msg),
                "message": record.getMessage(),
                "exc_type": exc_type,
                "location": f"{record.pathname.rsplit(os.sep, 1)[-1]}:{record.funcName}",
                "test": _current_test,
            }
        )


def pytest_configure(config):  # noqa: D401 - pytest hook
    global _exporter, _metric_reader
    handler = _CaptureHandler(level=logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.WARNING)
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )
    except ImportError:  # pragma: no cover - telemetry is optional
        return
    _exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(provider)

    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider, UpDownCounter
        from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
    except ImportError:  # pragma: no cover - metrics are optional
        return
    # Delta temporality: each collection holds only what was recorded since the
    # previous one, so collecting before and after a test isolates that test.
    delta = AggregationTemporality.DELTA
    _metric_reader = InMemoryMetricReader(
        preferred_temporality={Counter: delta, UpDownCounter: delta, Histogram: delta}
    )
    metrics.set_meter_provider(MeterProvider(metric_readers=[_metric_reader]))


def _collect_metrics(test: Optional[str]) -> None:
    """Drain the metric reader; keep the points when they belong to a test."""
    data = _metric_reader.get_metrics_data() if _metric_reader else None
    if not data or test is None:
        return
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name.startswith("otel."):  # the SDK's own metrics
                    continue
                kind = type(metric.data).__name__
                if kind == "Sum":
                    kind = "counter" if metric.data.is_monotonic else "updowncounter"
                for point in metric.data.data_points:
                    entry = {
                        "test": test,
                        "name": metric.name,
                        "kind": kind.lower(),
                        "unit": metric.unit or "",
                        "attributes": {k: str(v) for k, v in (point.attributes or {}).items()},
                    }
                    if kind == "Histogram":
                        entry["count"], entry["sum"] = point.count, point.sum
                    else:
                        entry["value"] = point.value
                    _metrics.append(entry)


def pytest_runtest_setup(item):
    global _current_test
    _current_test = item.nodeid


def pytest_runtest_teardown(item):
    global _current_test
    _current_test = None


_span_tests: Dict[int, Optional[str]] = {}


def pytest_runtest_call(item):
    # Remember how many spans existed before this test so we can attribute
    # new spans to it afterwards.
    item._confidence_span_start = len(_exporter.get_finished_spans()) if _exporter else 0
    _collect_metrics(None)  # drop anything recorded during setup
    if _track_memory:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        tracemalloc.reset_peak()
        item._confidence_mem_start = tracemalloc.get_traced_memory()[0]


def pytest_runtest_makereport(item, call):
    if call.when != "call":
        return
    _test_ids.append(item.nodeid)
    _collect_metrics(item.nodeid)
    if _track_memory and tracemalloc.is_tracing() and hasattr(item, "_confidence_mem_start"):
        _memory[item.nodeid] = max(0, tracemalloc.get_traced_memory()[1] - item._confidence_mem_start)
    if _exporter is None:
        return
    spans = _exporter.get_finished_spans()
    for span in spans[getattr(item, "_confidence_span_start", 0):]:
        _span_tests.setdefault(id(span), item.nodeid)


def _span_to_dict(span) -> Dict[str, Any]:
    events = []
    for ev in span.events:
        attrs = dict(ev.attributes or {})
        events.append(
            {
                "name": ev.name,
                "exc_type": attrs.get("exception.type"),
                "exc_message": attrs.get("exception.message"),
            }
        )
    status = getattr(span.status, "status_code", None)
    return {
        "name": span.name,
        "duration_ms": (span.end_time - span.start_time) / 1e6,
        "status": getattr(status, "name", str(status)),
        "events": events,
        "test": _span_tests.get(id(span)),
    }


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("CONFIDENCE_TELEMETRY_OUT")
    if not out:
        return
    spans = []
    if _exporter is not None:
        try:
            spans = [_span_to_dict(s) for s in _exporter.get_finished_spans()]
        except Exception:  # pragma: no cover - never break the test run
            traceback.print_exc()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {"spans": spans, "logs": _logs, "metrics": _metrics, "memory": _memory, "test_ids": _test_ids,
             "otel": _exporter is not None, "otel_metrics": _metric_reader is not None},
            fh, indent=1,
        )
