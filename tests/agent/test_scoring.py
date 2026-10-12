from confidence_agent.scoring import score
from confidence_agent.signals import Signals


def clean(**kw):
    """Signals for a change with nothing wrong, every check enabled."""
    kw.setdefault("tests_total", 10)
    kw.setdefault("otel_enabled", True)
    kw.setdefault("metrics_enabled", True)
    kw.setdefault("memory_enabled", True)
    return Signals(**kw)


def points(result, signal):
    return sum(p for s, p, _ in result.deductions if s == signal)


def test_clean_change_scores_100_with_every_check_listed():
    r = score(clean(changed_code_lines=4, changed_lines_covered=4))
    assert (r.value, r.band, r.deductions) == (100, "High", [])
    assert r.passed_checks == [
        "All 10 tests pass",
        "All 4 changed code lines run under tests",
        "No new ERROR logs",
        "No new exceptions in traces",
        "No latency regressions in traced operations",
        "No coverage exclusions, removed instrumentation or silenced errors",
        "No security-sensitive calls on changed lines",
        "No unusual changes in app metrics",
        "No memory growth in tests",
    ]


def test_disabled_checks_are_neither_passed_nor_deducted():
    r = score(Signals(tests_total=3))
    assert r.value == 100
    assert r.passed_checks == [
        "All 3 tests pass", "No new ERROR logs", "No coverage exclusions, removed instrumentation or silenced errors",
        "No security-sensitive calls on changed lines",
    ]


def test_newly_failing_tests_cost_45_then_5_each_up_to_55():
    assert points(score(clean(tests_newly_failing=["a"])), "Tests") == 45
    assert points(score(clean(tests_newly_failing=["a", "b"])), "Tests") == 50
    assert points(score(clean(tests_newly_failing=list("abcdef"))), "Tests") == 55


def test_removed_tests_cost_20_then_5_each():
    assert points(score(clean(tests_removed=["a"])), "Tests") == 20  # enough to leave High
    r = score(clean(tests_removed=["a", "b"]))
    assert points(r, "Tests") == 25
    assert "2 test(s) were removed" in r.deductions[0][2]


def test_uncovered_changed_lines_cost_up_to_30_proportionally():
    assert points(score(clean(changed_code_lines=10, changed_lines_covered=0)), "Coverage") == 30
    assert points(score(clean(changed_code_lines=10, changed_lines_covered=5)), "Coverage") == 15
    # One untested line out of 100 is still reported, never rounded away to "all covered".
    assert points(score(clean(changed_code_lines=100, changed_lines_covered=99)), "Coverage") == 1


def test_no_changed_code_lines_means_no_coverage_row():
    r = score(clean())
    assert not any("changed code lines" in c for c in r.passed_checks)
    assert points(r, "Coverage") == 0


def test_error_logs_cost_20_then_5_each_up_to_25():
    assert points(score(clean(new_error_logs={"a": 3})), "Logs") == 20
    assert points(score(clean(new_error_logs={"a": 1, "b": 1})), "Logs") == 25
    assert points(score(clean(new_error_logs={"a": 1, "b": 1, "c": 1})), "Logs") == 25


def test_span_exceptions_cost_10_then_5_each_up_to_15():
    assert points(score(clean(new_span_exceptions={"a": 1})), "Traces") == 10
    assert points(score(clean(new_span_exceptions={"a": 1, "b": 1, "c": 1})), "Traces") == 15


def test_trace_signals_are_skipped_without_otel():
    r = score(clean(otel_enabled=False, new_span_exceptions={"a": 1}, latency_regressions={"op": {}}))
    assert points(r, "Traces") == 0 and points(r, "Latency") == 0


def test_latency_costs_10_per_slower_span_up_to_20():
    assert points(score(clean(latency_regressions={"a": {}})), "Latency") == 10
    assert points(score(clean(latency_regressions={"a": {}, "b": {}, "c": {}})), "Latency") == 20


def test_metrics_cost_10_then_5_each_up_to_20():
    one = {"c": {"test": "t", "kind": "counter", "before": 1, "after": 5}}
    assert points(score(clean(metric_regressions=one)), "Metrics") == 10
    r = score(clean(metric_regressions=one, new_error_metrics={"errors": 2}))
    assert points(r, "Metrics") == 15
    assert "1 new error counter(s)" in r.deductions[0][2]
    many = {k: {} for k in "abcde"}
    assert points(score(clean(metric_regressions=many)), "Metrics") == 20


def test_metric_and_memory_signals_are_skipped_when_disabled():
    r = score(clean(metrics_enabled=False, memory_enabled=False,
                    metric_regressions={"c": {}}, memory_regressions={"t": {}}))
    assert r.value == 100


def test_memory_costs_10_per_test_up_to_20():
    assert points(score(clean(memory_regressions={"t1": {}})), "Memory") == 10
    assert points(score(clean(memory_regressions={"t1": {}, "t2": {}, "t3": {}})), "Memory") == 20


def test_large_diff_costs_5():
    assert points(score(clean(code_diff_lines=401)), "Size") == 5
    assert points(score(clean(code_diff_lines=400)), "Size") == 0


def test_bands():
    assert score(clean(latency_regressions={"a": {}})).band == "High"  # 90
    medium = clean(new_error_logs={"a": 1}, new_span_exceptions={"b": 1},
                   latency_regressions={"c": {}})
    assert score(medium).value == 60 and score(medium).band == "Medium"
    assert score(clean(tests_newly_failing=["a"])).band == "Low"  # 55


def test_score_never_goes_below_zero():
    r = score(clean(
        tests_newly_failing=list("abcdef"), tests_removed=["x"],
        changed_code_lines=10, new_error_logs={"a": 1, "b": 1},
        new_span_exceptions={"a": 1, "b": 1, "c": 1}, latency_regressions={"a": {}, "b": {}},
        metric_regressions={"a": {}, "b": {}, "c": {}}, memory_regressions={"a": {}, "b": {}},
        diff_lines=1000,
    ))
    assert r.value == 0
    assert r.band == "Low"
