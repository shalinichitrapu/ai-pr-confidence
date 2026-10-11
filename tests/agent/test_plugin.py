"""The pytest plugin and runner, end to end on a small instrumented project."""

from pathlib import Path

import pytest

from confidence_agent.runner import run_tests

APP = '''
import logging
from opentelemetry import metrics, trace

tracer = trace.get_tracer("app")
meter = metrics.get_meter("app")
log = logging.getLogger("app")
calls = meter.create_counter("app.calls")
size = meter.create_histogram("app.size")


def work(n, fail=False):
    with tracer.start_as_current_span("app.work"):
        calls.add(n, {"mode": "fail" if fail else "ok"})
        size.record(n)
        if fail:
            try:
                raise ValueError("bad")
            except ValueError:
                log.exception("swallowed %s", n)
        return [0] * n


def explode():
    with tracer.start_as_current_span("app.explode"):
        raise KeyError("x")
'''

TESTS = '''
import pytest
from app import calls, explode, work


@pytest.fixture
def warm():
    calls.add(100, {"mode": "ok"})  # recorded during setup: must not count for the test


def test_small(warm):
    work(2)


def test_fails_quietly():
    work(1, fail=True)


def test_big():
    work(2_000_000)


def test_broken():
    explode()


@pytest.mark.skip(reason="not run")
def test_skipped():
    pass
'''

SMALL = "tests/test_app.py::test_small"
QUIET = "tests/test_app.py::test_fails_quietly"
BIG = "tests/test_app.py::test_big"
BROKEN = "tests/test_app.py::test_broken"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    (root / "app.py").write_text(APP)
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(TESTS)
    return run_tests(root, "head", "app", "tests")


def test_outcomes_from_junit(result):
    assert result.tests == {
        "tests.test_app::test_small": "passed",
        "tests.test_app::test_fails_quietly": "passed",
        "tests.test_app::test_big": "passed",
        "tests.test_app::test_broken": "failed",
        "tests.test_app::test_skipped": "skipped",
    }
    assert result.failed == ["tests.test_app::test_broken"]


def test_node_ids_cover_tests_that_ran(result):
    assert result.node_ids == [SMALL, QUIET, BIG, BROKEN]


def test_otel_providers_were_installed(result):
    assert result.otel_enabled and result.otel_metrics_enabled


def test_spans_are_tagged_with_their_test(result):
    by_test = {s["test"]: s for s in result.spans}
    assert set(by_test) == {SMALL, QUIET, BIG, BROKEN}
    assert by_test[BROKEN]["name"] == "app.explode"
    assert [e["exc_type"] for e in by_test[BROKEN]["events"]] == ["KeyError"]
    # The ValueError was caught inside the span, so the span itself is clean.
    assert by_test[QUIET]["events"] == []


def test_swallowed_error_is_captured_as_a_log(result):
    errors = [r for r in result.logs if r["level"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["test"] == QUIET
    assert errors[0]["template"] == "swallowed %s"
    assert errors[0]["exc_type"] == "ValueError"
    assert errors[0]["location"] == "app.py:work"


def test_metrics_are_per_test_and_exclude_setup(result):
    calls = {(m["test"], m["attributes"]["mode"]): m["value"]
             for m in result.metrics if m["name"] == "app.calls"}
    assert calls == {(SMALL, "ok"): 2, (QUIET, "fail"): 1, (BIG, "ok"): 2_000_000}
    hist = {m["test"]: (m["count"], m["sum"]) for m in result.metrics if m["name"] == "app.size"}
    assert hist[SMALL] == (1, 2)
    assert not any(m["name"].startswith("otel.") for m in result.metrics)


def test_peak_memory_is_per_test(result):
    assert set(result.memory) == {SMALL, QUIET, BIG, BROKEN}
    assert result.memory[BIG] > 10 * 1024 * 1024  # a list of 2 million references
    assert result.memory[SMALL] < 1024 * 1024


def test_coverage_is_collected(result):
    assert result.coverage["app.py"]["missing_lines"] == []


def test_memory_tracking_can_be_turned_off(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_f.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    result = run_tests(tmp_path, "head", "app", "tests", track_memory=False)
    assert result.memory == {}
    assert result.node_ids == ["tests/test_f.py::test_f"]
