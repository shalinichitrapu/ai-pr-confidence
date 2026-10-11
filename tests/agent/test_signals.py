from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult
from confidence_agent.signals import _in_source, compute

T1 = "tests/test_a.py::test_one"
T2 = "tests/test_a.py::test_two"


def run(**kw):
    kw.setdefault("tests", {"t.a::one": "passed", "t.a::two": "passed"})
    kw.setdefault("node_ids", [T1, T2])
    return RunResult(label="x", **kw)


def signals(base=None, head=None, diff=None, source="."):
    return compute(base or run(), head or run(), diff or Diff(), "tests", source)


def error_log(template="boom", level="ERROR", exc_type=None):
    return {"level": level, "logger": "app", "template": template, "message": template,
            "exc_type": exc_type, "location": "a.py:f", "test": T1}


def span(name="op", ms=1.0, exc=None):
    events = [{"name": "exception", "exc_type": exc, "exc_message": ""}] if exc else []
    return {"name": name, "duration_ms": ms, "status": "UNSET", "events": events, "test": T1}


def point(name, value, kind="counter", test=T1, **attrs):
    return {"test": test, "name": name, "kind": kind, "unit": "", "attributes": attrs, "value": value}


def hist(name, count, total, test=T1):
    return {"test": test, "name": name, "kind": "histogram", "unit": "", "attributes": {},
            "count": count, "sum": total}


# --- tests ---------------------------------------------------------------

def test_only_tests_that_passed_before_count_as_newly_failing():
    base = run(tests={"a": "passed", "b": "failed"})
    head = run(tests={"a": "failed", "b": "failed"})
    s = signals(base, head)
    assert s.tests_newly_failing == ["a"]
    assert s.tests_failed == ["a", "b"]


def test_errors_count_as_failures():
    s = signals(run(tests={"a": "passed"}), run(tests={"a": "error"}))
    assert s.tests_newly_failing == ["a"]


def test_removed_and_added_tests():
    s = signals(run(tests={"a": "passed", "b": "passed"}), run(tests={"a": "passed", "c": "passed"}))
    assert s.tests_removed == ["b"]
    assert s.tests_added == ["c"]
    assert s.tests_total == 2


# --- coverage of changed lines --------------------------------------------

def cov(executed, missing):
    return {"executed_lines": executed, "missing_lines": missing}


def test_changed_line_coverage_counts_only_executable_lines():
    head = run(coverage={"pkg/a.py": cov([1, 2], [3])})
    diff = Diff(added_lines={"pkg/a.py": {1, 3, 4}})  # line 4 is a comment/blank
    s = signals(head=head, diff=diff)
    assert s.changed_code_lines == 2
    assert s.changed_lines_covered == 1
    assert s.uncovered_changes == {"pkg/a.py": [3]}
    assert s.changed_line_coverage == 0.5


def test_file_in_source_that_no_test_imports_is_fully_uncovered():
    diff = Diff(added_lines={"pkg/new.py": {1, 2, 3}})
    s = signals(diff=diff, source="pkg")
    assert s.changed_code_lines == 3
    assert s.uncovered_changes == {"pkg/new.py": [1, 2, 3]}


def test_files_outside_source_are_not_coverage_checked():
    diff = Diff(added_lines={"scripts/tool.py": {1, 2}, "setup.py": {5}})
    s = signals(diff=diff, source="pkg")
    assert s.changed_code_lines == 0
    assert s.uncovered_changes == {}


def test_test_files_and_non_python_files_are_ignored():
    diff = Diff(added_lines={
        "tests/test_x.py": {1}, "pkg/test_y.py": {1}, "pkg/conftest.py": {1}, "README.md": {1},
    })
    assert signals(diff=diff).changed_code_lines == 0


def test_no_changed_code_means_full_coverage():
    assert signals().changed_line_coverage == 1.0


def test_in_source_matching():
    assert _in_source("pkg/a.py", "pkg")
    assert _in_source("pkg/a.py", "./pkg/")
    assert not _in_source("pkgx/a.py", "pkg")
    assert _in_source("anything.py", ".")
    assert _in_source("src/my/pkg/a.py", "src/my/pkg")
    assert _in_source("my/pkg/a.py", "my.pkg")
    assert _in_source("lib/b.py", "pkg,lib")
    assert _in_source("lib/b.py", "pkg, lib")
    assert not _in_source("other/c.py", "pkg,lib")


# --- logs and traces -----------------------------------------------------

def test_new_error_log_signatures_are_flagged():
    base = run(logs=[error_log("old problem")])
    head = run(logs=[error_log("old problem"), error_log("new %s", exc_type="ValueError"),
                     error_log("new %s", exc_type="ValueError")])
    s = signals(base, head)
    assert s.new_error_logs == {"app a.py:f: ValueError": 2}


def test_warnings_are_not_error_logs():
    s = signals(head=run(logs=[error_log("careful", level="WARNING")]))
    assert s.new_error_logs == {}


def test_new_span_exceptions_are_flagged():
    base = run(spans=[span(exc="KeyError")])
    head = run(spans=[span(exc="KeyError"), span(exc="ValueError")])
    assert signals(base, head).new_span_exceptions == {"op: ValueError": 1}


def test_latency_regression_needs_both_ratio_and_real_delta():
    base = run(spans=[span("slow", 10.0), span("tiny", 1.0), span("ok", 10.0)])
    head = run(spans=[span("slow", 25.0), span("tiny", 3.0), span("ok", 15.0), span("brand_new", 99.0)])
    s = signals(base, head)
    assert list(s.latency_regressions) == ["slow"]
    assert s.latency_regressions["slow"] == {"before_ms": 10.0, "after_ms": 25.0}


# --- metrics ---------------------------------------------------------------

def metric_signals(base_points, head_points, **kw):
    base = run(metrics=base_points, otel_metrics_enabled=True, **kw)
    head = run(metrics=head_points, otel_metrics_enabled=True, **kw)
    return signals(base, head)


def test_counter_that_doubles_and_grows_by_three_is_flagged():
    s = metric_signals([point("db.queries", 2)], [point("db.queries", 6)])
    assert s.metrics_enabled
    assert s.metric_regressions == {
        "db.queries": {"test": T1, "kind": "counter", "before": 2, "after": 6}
    }


def test_small_counter_growth_is_not_flagged():
    # Doubled, but only +2.
    assert metric_signals([point("c", 2)], [point("c", 4)]).metric_regressions == {}
    # +5, but not doubled.
    assert metric_signals([point("c", 10)], [point("c", 15)]).metric_regressions == {}


def test_series_are_compared_per_attribute_set():
    s = metric_signals(
        [point("answers", 1, result="ok"), point("answers", 1, result="missed")],
        [point("answers", 1, result="ok"), point("answers", 5, result="missed")],
    )
    assert list(s.metric_regressions) == ["answers{result=missed}"]


def test_counter_points_in_one_test_are_summed():
    s = metric_signals([point("c", 1)], [point("c", 2), point("c", 2)])
    assert s.metric_regressions["c"]["after"] == 4


def test_worst_test_is_reported_for_a_series():
    s = metric_signals(
        [point("c", 1, test=T1), point("c", 1, test=T2)],
        [point("c", 5, test=T1), point("c", 9, test=T2)],
    )
    assert s.metric_regressions["c"]["test"] == T2


def test_histograms_compare_the_mean():
    s = metric_signals([hist("latency", 2, 10.0)], [hist("latency", 4, 80.0)])
    assert s.metric_regressions["latency"]["kind"] == "histogram mean"
    assert (s.metric_regressions["latency"]["before"], s.metric_regressions["latency"]["after"]) == (5.0, 20.0)
    # More recordings with the same mean are not a regression.
    assert metric_signals([hist("h", 2, 10.0)], [hist("h", 8, 40.0)]).metric_regressions == {}


def test_gauges_compare_the_last_value():
    s = metric_signals([point("queue", 1, kind="gauge"), point("queue", 3, kind="gauge")],
                       [point("queue", 1, kind="gauge"), point("queue", 9, kind="gauge")])
    assert s.metric_regressions["queue"]["before"] == 3
    assert s.metric_regressions["queue"]["after"] == 9


def test_new_error_like_counter_is_flagged():
    s = metric_signals([], [point("app.retries", 2), point("app.calls", 1, status="timeout")])
    assert s.new_error_metrics == {"app.retries": 2, "app.calls{status=timeout}": 1}


def test_new_ordinary_counter_is_not_flagged():
    s = metric_signals([], [point("app.cache_hits", 50)])
    assert s.new_error_metrics == {}
    assert s.metric_regressions == {}


def test_metrics_from_tests_that_only_exist_in_head_are_ignored():
    base = run(metrics=[], node_ids=[T1], otel_metrics_enabled=True)
    head = run(metrics=[point("errors", 3, test=T2)], node_ids=[T1, T2], otel_metrics_enabled=True)
    assert signals(base, head).new_error_metrics == {}


def test_metrics_disabled_when_nothing_was_recorded():
    assert not metric_signals([], []).metrics_enabled


# --- memory ------------------------------------------------------------------

MB = 1024 * 1024


def test_memory_regression_needs_both_ratio_and_five_mb():
    base = run(memory={T1: 1 * MB, T2: 20 * MB})
    head = run(memory={T1: 8 * MB, T2: 30 * MB})  # T2 grew 10 MB but not 2x
    s = signals(base, head)
    assert s.memory_enabled
    assert s.memory_regressions == {T1: {"before_mb": 1.0, "after_mb": 8.0}}


def test_small_memory_growth_is_not_flagged():
    s = signals(run(memory={T1: 100_000}), run(memory={T1: 900_000}))
    assert s.memory_regressions == {}


def test_memory_disabled_without_measurements():
    assert not signals().memory_enabled


# --- diff size -----------------------------------------------------------------

def test_diff_size_and_files_are_copied():
    diff = Diff(files_changed=["a.py", "b.md"], lines_added=10, lines_removed=4)
    s = signals(diff=diff)
    assert s.diff_lines == 14
    assert s.files_changed == ["a.py", "b.md"]
