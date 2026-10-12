"""Deterministic confidence score.

The score starts at 100 and loses points for each kind of evidence that the
change is risky. Every deduction comes with a human-readable reason, so the
number can always be explained. The LLM never changes the score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Config
from .signals import Signals


@dataclass
class Score:
    value: int
    band: str
    deductions: List[Tuple[str, int, str]] = field(default_factory=list)  # (signal, points, reason)
    passed_checks: List[str] = field(default_factory=list)
    # (signal, points, reason, who) for deductions a reviewer acknowledged: shown, not counted
    acknowledged: List[Tuple[str, int, str, str]] = field(default_factory=list)
    keys: List[str] = field(default_factory=list)  # acknowledgement key of each deduction, in order


# Deductions a reviewer can't wave through: a broken or unmeasured change has to be fixed.
UNACKABLE = {"tests-failing", "no-tests"}
# Every acknowledgement key, one per kind of deduction.
KEYS = {"no-tests", "tests-failing", "tests-flaky", "tests-removed", "coverage", "mutation", "logs",
        "logs-new-tests", "traces-new-tests", "traces", "latency", "visibility", "security", "dependencies",
        "metrics", "memory", "size"}


def score(s: Signals, config: Optional[Config] = None, acks: Optional[Dict[str, str]] = None) -> Score:
    """acks: {deduction key: who acknowledged it} for deductions to show but not count.

    Each kind of deduction has its own key (e.g. "tests-removed", "metrics"), so
    acknowledging one never hides another that shares its display name.
    """
    cfg = config or Config()
    w = cfg.weights
    deductions: List[Tuple[str, int, str]] = []
    keys: List[str] = []
    passed: List[str] = []

    def add(key: str, deduction: Tuple[str, int, str]) -> None:
        keys.append(key)
        deductions.append(deduction)

    # 1. Tests. A newly failing test alone takes the score below Medium (60).
    if s.tests_total == 0:
        add("no-tests", ("Tests", w.no_tests.points(1), "No test results: tests didn't run or couldn't be collected"))
    elif s.tests_newly_failing:
        pts = w.tests_failing.points(len(s.tests_newly_failing))
        add("tests-failing", ("Tests", pts, f"{len(s.tests_newly_failing)} test(s) fail that passed before"))
    elif s.tests_failed:
        passed.append(
            f"No test newly fails ({len(s.tests_failed)} of {s.tests_total} already failed before this change)"
        )
    else:
        passed.append(f"All {s.tests_total} tests pass")
    if s.tests_flaky:
        add("tests-flaky", ("Tests", w.tests_flaky.points(len(s.tests_flaky)),
                           f"{len(s.tests_flaky)} test(s) failed, then passed on a rerun "
                           "(flaky, or made flaky by this change)"))
    if s.tests_removed:
        add("tests-removed", ("Tests", w.tests_removed.points(len(s.tests_removed)),
                           f"{len(s.tests_removed)} test(s) were removed"))

    # 2. Coverage of the lines this change touched
    if s.changed_code_lines:
        pct = s.changed_line_coverage
        pts = round((1 - pct) * w.coverage.cap)
        if pts:
            add("coverage", 
                ("Coverage", pts, f"Only {pct:.0%} of {s.changed_code_lines} changed code lines run under tests")
            )
        else:
            passed.append(f"All {s.changed_code_lines} changed code lines run under tests")

    # 2b. Do the tests check the changed lines, or only run them?
    if s.mutants_tested:
        rate = len(s.mutants_survived) / s.mutants_tested
        pts = round(rate * w.mutation.cap)
        if pts:
            add("mutation", ("Mutation", pts, f"{len(s.mutants_survived)} of {s.mutants_tested} small changes "
                               "to the changed lines went unnoticed by every test that runs them"))
        else:
            passed.append(f"Tests caught all {s.mutants_tested} small changes made to the changed lines")

    # 3. Runtime evidence from logs and traces
    # A new swallowed error on its own is enough to drop out of High, even in a
    # project without tracing to back it up.
    if s.new_error_logs:
        pts = w.error_logs.points(len(s.new_error_logs))
        total = sum(s.new_error_logs.values())
        add("logs", ("Logs", pts, f"{total} new ERROR log(s) across {len(s.new_error_logs)} signature(s), a sign of errors being caught and hidden"))
    elif not s.new_test_error_logs:
        passed.append("No new ERROR logs")
    # Errors seen only in tests this change adds: half weight. They may be what
    # the new test checks, but they're also the only place a new feature's
    # swallowed errors show up. Tests that expect an exception aren't counted.
    if s.new_test_error_logs:
        pts = math.ceil(w.error_logs.points(len(s.new_test_error_logs)) / 2)
        total = sum(s.new_test_error_logs.values())
        add("logs-new-tests", ("Logs", pts, f"{total} new ERROR log(s) only in tests this change adds "
                                        "(half weight; check they're expected)"))
    if s.otel_enabled and s.new_test_span_exceptions:
        pts = math.ceil(w.span_exceptions.points(len(s.new_test_span_exceptions)) / 2)
        total = sum(s.new_test_span_exceptions.values())
        add("traces-new-tests", ("Traces", pts, f"{total} new exception(s) on spans only in tests this change adds "
                                          "(half weight)"))
    if s.otel_enabled:
        if s.new_span_exceptions:
            pts = w.span_exceptions.points(len(s.new_span_exceptions))
            total = sum(s.new_span_exceptions.values())
            add("traces", ("Traces", pts, f"{total} new exception(s) recorded on spans"))
        elif not s.new_test_span_exceptions:
            passed.append("No new exceptions in traces")
        if s.latency_regressions:
            pts = w.latency.points(len(s.latency_regressions))
            names = ", ".join(s.latency_regressions)
            add("latency", ("Latency", pts, f"Slower spans: {names}"))
        elif not s.latency_skipped:
            passed.append("No latency regressions in traced operations")

    # 4. Changes that hide code from the other checks
    if s.hidden_code:
        kinds = ", ".join(s.hidden_code)
        add("visibility", ("Visibility", w.visibility.points(len(s.hidden_code)),
                           f"Code hidden from these checks: {kinds}"))
    else:
        passed.append("No coverage exclusions, removed instrumentation or silenced errors")

    # 4b. What tests can't judge: risky calls and new dependencies
    if s.risky_code:
        add("security", ("Security", w.security.points(len(s.risky_code)),
                           f"{len(s.risky_code)} security-sensitive call(s) or value(s) on changed lines"))
    else:
        passed.append("No security-sensitive calls on changed lines")
    if s.dependency_changes:
        add("dependencies", ("Dependencies", w.dependencies.points(len(s.dependency_changes)),
                           f"{len(s.dependency_changes)} dependency added or changed; review it before merging"))

    # 5. Metrics and memory, compared within the same test
    if s.metrics_enabled:
        flagged = len(s.metric_regressions) + len(s.new_error_metrics)
        if flagged:
            parts = []
            if s.metric_regressions:
                parts.append(f"{len(s.metric_regressions)} metric(s) grew at least "
                             f"{cfg.thresholds.metric_ratio:g}× in the same test")
            if s.new_error_metrics:
                parts.append(f"{len(s.new_error_metrics)} new error counter(s)")
            add("metrics", ("Metrics", w.metrics.points(flagged), "; ".join(parts)))
        else:
            passed.append("No unusual changes in app metrics")
    if s.memory_enabled:
        if s.memory_regressions:
            pts = w.memory.points(len(s.memory_regressions))
            add("memory", ("Memory", pts, f"{len(s.memory_regressions)} test(s) use at least "
                               f"{cfg.thresholds.memory_ratio:g}× more peak memory"))
        else:
            passed.append("No memory growth in tests")

    # 6. Reviewability
    if s.code_diff_lines > cfg.thresholds.large_diff_lines:
        add("size", 
            ("Size", w.size.points(1), f"Large change ({s.code_diff_lines} lines, not counting tests) is harder to review")
        )

    kept = [(k, d) for k, d in zip(keys, deductions) if d[1] > 0]  # a weight set to 0 turns that check off
    acks = {k.lower(): v for k, v in (acks or {}).items() if k.lower() not in UNACKABLE}
    acknowledged = [(*d, acks[k]) for k, d in kept if k in acks]
    kept = [(k, d) for k, d in kept if k not in acks]
    keys, deductions = [k for k, _ in kept], [d for _, d in kept]
    value = max(0, 100 - sum(p for _, p, _ in deductions))
    band = "High" if value >= cfg.bands.high else "Medium" if value >= cfg.bands.medium else "Low"
    return Score(value=value, band=band, deductions=deductions, passed_checks=passed, acknowledged=acknowledged,
                 keys=keys)
