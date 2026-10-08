"""pytest plugin loaded by the confidence agent during each test run.

It installs an OpenTelemetry SDK tracer provider that keeps spans in memory,
captures WARNING+ log records, tags everything with the test that produced it,
and writes the result to the JSON file named by CONFIDENCE_TELEMETRY_OUT.

The code under test only needs the OpenTelemetry API; nothing in the target
repository has to change.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any, Dict, List, Optional

_current_test: Optional[str] = None
_logs: List[Dict[str, Any]] = []
_exporter = None


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
    global _exporter
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


def pytest_runtest_makereport(item, call):
    if call.when != "call" or _exporter is None:
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
        json.dump({"spans": spans, "logs": _logs, "otel": _exporter is not None}, fh, indent=1)
