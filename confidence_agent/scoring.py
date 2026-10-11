"""Deterministic confidence score.

The score starts at 100 and loses points for each kind of evidence that the
change is risky. Every deduction comes with a human-readable reason, so the
number can always be explained. The LLM never changes the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import Config
from .signals import Signals


@dataclass
class Score:
    value: int
    band: str
    deductions: List[Tuple[str, int, str]] = field(default_factory=list)  # (signal, points, reason)
    passed_checks: List[str] = field(default_factory=list)


def score(s: Signals, config: Optional[Config] = None) -> Score:
    cfg = config or Config()
    w = cfg.weights
    deductions: List[Tuple[str, int, str]] = []
    passed: List[str] = []

    # 1. Tests. A newly failing test alone takes the score below Medium (60).
    if s.tests_total == 0:
        deductions.append(("Tests", w.no_tests.points(1), "No test results: tests didn't run or couldn't be collected"))
    elif s.tests_newly_failing:
        pts = w.tests_failing.points(len(s.tests_newly_failing))
        deductions.append(("Tests", pts, f"{len(s.tests_newly_failing)} test(s) fail that passed before"))
    elif s.tests_failed:
        passed.append(
            f"No test newly fails ({len(s.tests_failed)} of {s.tests_total} already failed before this change)"
        )
    else:
        passed.append(f"All {s.tests_total} tests pass")
    if s.tests_removed:
        deductions.append(("Tests", w.tests_removed.points(len(s.tests_removed)),
                           f"{len(s.tests_removed)} test(s) were removed"))

    # 2. Coverage of the lines this change touched
    if s.changed_code_lines:
        pct = s.changed_line_coverage
        pts = round((1 - pct) * w.coverage.cap)
        if pts:
            deductions.append(
                ("Coverage", pts, f"Only {pct:.0%} of {s.changed_code_lines} changed code lines run under tests")
            )
        else:
            passed.append(f"All {s.changed_code_lines} changed code lines run under tests")

    # 3. Runtime evidence from logs and traces
    # A new swallowed error on its own is enough to drop out of High, even in a
    # project without tracing to back it up.
    if s.new_error_logs:
        pts = w.error_logs.points(len(s.new_error_logs))
        total = sum(s.new_error_logs.values())
        deductions.append(("Logs", pts, f"{total} new ERROR log(s) across {len(s.new_error_logs)} signature(s), a sign of errors being caught and hidden"))
    else:
        passed.append("No new ERROR logs")
    if s.otel_enabled:
        if s.new_span_exceptions:
            pts = w.span_exceptions.points(len(s.new_span_exceptions))
            total = sum(s.new_span_exceptions.values())
            deductions.append(("Traces", pts, f"{total} new exception(s) recorded on spans"))
        else:
            passed.append("No new exceptions in traces")
        if s.latency_regressions:
            pts = w.latency.points(len(s.latency_regressions))
            names = ", ".join(s.latency_regressions)
            deductions.append(("Latency", pts, f"Slower spans: {names}"))
        else:
            passed.append("No latency regressions in traced operations")

    # 4. Changes that hide code from the other checks
    if s.hidden_code:
        kinds = ", ".join(s.hidden_code)
        deductions.append(("Visibility", w.visibility.points(len(s.hidden_code)),
                           f"Code hidden from these checks: {kinds}"))
    else:
        passed.append("No coverage exclusions added or instrumentation removed")

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
            deductions.append(("Metrics", w.metrics.points(flagged), "; ".join(parts)))
        else:
            passed.append("No unusual changes in app metrics")
    if s.memory_enabled:
        if s.memory_regressions:
            pts = w.memory.points(len(s.memory_regressions))
            deductions.append(("Memory", pts, f"{len(s.memory_regressions)} test(s) use at least "
                               f"{cfg.thresholds.memory_ratio:g}× more peak memory"))
        else:
            passed.append("No memory growth in tests")

    # 6. Reviewability
    if s.code_diff_lines > cfg.thresholds.large_diff_lines:
        deductions.append(
            ("Size", w.size.points(1), f"Large change ({s.code_diff_lines} lines, not counting tests) is harder to review")
        )

    deductions = [d for d in deductions if d[1] > 0]  # a weight set to 0 turns that check off
    value = max(0, 100 - sum(p for _, p, _ in deductions))
    band = "High" if value >= cfg.bands.high else "Medium" if value >= cfg.bands.medium else "Low"
    return Score(value=value, band=band, deductions=deductions, passed_checks=passed)
