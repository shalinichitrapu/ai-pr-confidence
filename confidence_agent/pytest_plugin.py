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
import sys
import traceback
import tracemalloc
from typing import Any, Dict, List, Optional

import pytest

_current_test: Optional[str] = None
_span_tests: Dict[int, Optional[str]] = {}  # span id -> test running when the span started


def _test_now() -> Optional[str]:
    # The test running right now. Deliberately not a context variable: a
    # long-lived asyncio task (a server or queue worker started by a shared
    # fixture) would carry the first test's value forever and get every later
    # test's errors credited to it.
    return _current_test
_logs: List[Dict[str, Any]] = []
_exporter = None
_metric_reader = None
_metrics: List[Dict[str, Any]] = []
_memory: Dict[str, int] = {}  # test id -> peak bytes allocated during the test
_test_ids: List[str] = []  # pytest node ids, the ids metrics and memory are keyed by
_track_memory = os.environ.get("CONFIDENCE_MEMORY", "1") != "0"


def _location(record: logging.LogRecord) -> str:
    """file:function, with the file relative to the checkout so two payments.py don't collide."""
    workdir = os.environ.get("CONFIDENCE_WORKDIR")
    path = os.path.realpath(record.pathname)
    if workdir and path.startswith(os.path.realpath(workdir) + os.sep):
        name = os.path.relpath(path, os.path.realpath(workdir)).replace(os.sep, "/")
    else:
        name = record.pathname.rsplit(os.sep, 1)[-1]
    return f"{name}:{record.funcName}"


def _capture(record: logging.LogRecord) -> None:
    exc_type = None
    if record.exc_info and record.exc_info[0] is not None:
        exc_type = record.exc_info[0].__name__
    try:
        message = record.getMessage()
    except Exception:  # pragma: no cover - a bad format string must not break the test run
        message = str(record.msg)
    _logs.append(
        {
            "level": record.levelname,
            "logger": record.name,
            # The unformatted template groups repeats of the same message.
            "template": str(record.msg),
            "message": message,
            "exc_type": exc_type,
            "location": _location(record),
            "test": _test_now(),
        }
    )


_original_handle = logging.Logger.handle


def _handle(self, record):
    # Captured where every logger passes, not through a root handler: a project's
    # logging.config.dictConfig can remove handlers, and propagate=False loggers
    # never reach the root.
    if record.levelno >= logging.WARNING and not self.disabled and not getattr(record, "_confidence_seen", False):
        record._confidence_seen = True
        _capture(record)
    return _original_handle(self, record)


def _tag_span(span, parent_context=None) -> None:
    """Credit a span to the test running when it *started*, not when it ended.

    A span started by one test in a background thread may finish during the next
    test. Child spans aren't given their parent's test: a long-lived worker
    started inside a span would otherwise credit every later test's work to the
    test that started it.
    """
    try:
        _span_tests[span.get_span_context().span_id] = _test_now()
    except Exception:  # pragma: no cover - never break the code under test
        pass


def pytest_configure(config):  # noqa: D401 - pytest hook
    global _exporter, _metric_reader
    logging.Logger.handle = _handle
    root = logging.getLogger()
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
    from opentelemetry.sdk.trace import SpanProcessor

    class _TestTagger(SpanProcessor):
        def on_start(self, span, parent_context=None):
            _tag_span(span, parent_context)

    existing = trace.get_tracer_provider()
    if type(existing).__name__ == "ProxyTracerProvider":  # nothing set yet
        target = provider
        trace.set_tracer_provider(provider)
    else:
        # The project set up OpenTelemetry itself (e.g. in conftest.py). Only one
        # provider can be global, so add our processors to its provider if we can.
        target = existing if hasattr(existing, "add_span_processor") else None
    if target is None:
        _exporter = None  # can't observe spans; trace checks are reported as off
    else:
        target.add_span_processor(_TestTagger())
        target.add_span_processor(SimpleSpanProcessor(_exporter))

    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import (
            Counter, Histogram, MeterProvider, ObservableCounter, ObservableUpDownCounter, UpDownCounter,
        )
        from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
    except ImportError:  # pragma: no cover - metrics are optional
        return
    # Delta temporality: each collection holds only what was recorded since the
    # previous one, so collecting before and after a test isolates that test.
    if "Proxy" not in type(metrics.get_meter_provider()).__name__:
        return  # the project set its own meter provider; readers can't be added to it, so metrics are off
    delta = AggregationTemporality.DELTA
    _metric_reader = InMemoryMetricReader(
        preferred_temporality={Counter: delta, UpDownCounter: delta, Histogram: delta,
                               ObservableCounter: delta, ObservableUpDownCounter: delta}
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


def _coverage_context(name: str) -> None:
    """Tag coverage data with the running test, so each line knows which tests ran it."""
    try:
        import coverage
        cov = coverage.Coverage.current()
        if cov is not None:
            cov.switch_context(name)
    except Exception:  # pragma: no cover - never break the test run
        pass


def pytest_runtest_setup(item):
    global _current_test
    _current_test = item.nodeid
    _coverage_context(item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    # Fixture finalizers run inside pytest's own teardown, so keep crediting the
    # test until they're done.
    yield
    global _current_test
    _current_test = None
    _coverage_context("")


def pytest_runtest_call(item):
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
        "test": _span_tests.get(span.context.span_id),
    }


def _outside_imports() -> List[str]:
    """Modules loaded from the user's repository rather than the checkout under test.

    If this happens, the run measured the wrong version of the code.
    """
    repo, workdir = os.environ.get("CONFIDENCE_REPO"), os.environ.get("CONFIDENCE_WORKDIR")
    if not repo or not workdir:
        return []
    repo, workdir = os.path.realpath(repo), os.path.realpath(workdir)
    # Installed packages are not the project's code, even when the virtual
    # environment lives inside the repository (e.g. .venv/).
    envs = {os.path.realpath(p) + os.sep for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    found = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        path = os.path.realpath(path)
        if not path.startswith(repo + os.sep) or path.startswith(workdir + os.sep):
            continue
        parts = path.split(os.sep)
        if any(path.startswith(env) for env in envs) or "site-packages" in parts or "dist-packages" in parts:
            continue
        found.add(os.path.relpath(path, repo))
    return sorted(found)


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
             "otel": _exporter is not None, "otel_metrics": _metric_reader is not None,
             "outside_imports": _outside_imports()},
            fh, indent=1,
        )
