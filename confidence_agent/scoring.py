"""Deterministic confidence score.

The score starts at 100 and loses points for each kind of evidence that the
change is risky. Every deduction comes with a human-readable reason, so the
number can always be explained. The LLM never changes the score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .signals import Signals

HIGH, MEDIUM = 85, 60
LARGE_DIFF_LINES = 400


@dataclass
class Score:
    value: int
    band: str
    deductions: List[Tuple[str, int, str]] = field(default_factory=list)  # (signal, points, reason)
    passed_checks: List[str] = field(default_factory=list)


def _tiered(count: int, first: int, each: int, cap: int) -> int:
    if count <= 0:
        return 0
    return min(cap, first + each * (count - 1))


def score(s: Signals) -> Score:
    deductions: List[Tuple[str, int, str]] = []
    passed: List[str] = []

    # 1. Tests
    if s.tests_newly_failing:
        pts = _tiered(len(s.tests_newly_failing), 40, 5, 50)
        deductions.append(("Tests", pts, f"{len(s.tests_newly_failing)} test(s) fail that passed before"))
    else:
        passed.append(f"All {s.tests_total} tests pass")
    if s.tests_removed:
        deductions.append(("Tests", 15, f"{len(s.tests_removed)} test(s) were removed"))

    # 2. Coverage of the lines this change touched
    if s.changed_code_lines:
        pct = s.changed_line_coverage
        pts = round((1 - pct) * 30)
        if pts:
            deductions.append(
                ("Coverage", pts, f"Only {pct:.0%} of {s.changed_code_lines} changed code lines run under tests")
            )
        else:
            passed.append(f"All {s.changed_code_lines} changed code lines run under tests")

    # 3. Runtime evidence from logs and traces
    if s.new_error_logs:
        pts = _tiered(len(s.new_error_logs), 15, 5, 20)
        total = sum(s.new_error_logs.values())
        deductions.append(("Logs", pts, f"{total} new ERROR log(s) across {len(s.new_error_logs)} signature(s), a sign of errors being caught and hidden"))
    else:
        passed.append("No new ERROR logs")
    if s.otel_enabled:
        if s.new_span_exceptions:
            pts = _tiered(len(s.new_span_exceptions), 10, 5, 15)
            total = sum(s.new_span_exceptions.values())
            deductions.append(("Traces", pts, f"{total} new exception(s) recorded on spans"))
        else:
            passed.append("No new exceptions in traces")
        if s.latency_regressions:
            pts = min(20, 10 * len(s.latency_regressions))
            names = ", ".join(s.latency_regressions)
            deductions.append(("Latency", pts, f"Slower spans: {names}"))
        else:
            passed.append("No latency regressions in traced operations")

    # 4. Metrics and memory, compared within the same test
    if s.metrics_enabled:
        flagged = len(s.metric_regressions) + len(s.new_error_metrics)
        if flagged:
            parts = []
            if s.metric_regressions:
                parts.append(f"{len(s.metric_regressions)} metric(s) at least doubled in the same test")
            if s.new_error_metrics:
                parts.append(f"{len(s.new_error_metrics)} new error counter(s)")
            deductions.append(("Metrics", _tiered(flagged, 10, 5, 20), "; ".join(parts)))
        else:
            passed.append("No unusual changes in app metrics")
    if s.memory_enabled:
        if s.memory_regressions:
            pts = min(20, 10 * len(s.memory_regressions))
            deductions.append(("Memory", pts, f"{len(s.memory_regressions)} test(s) use at least 2× more peak memory"))
        else:
            passed.append("No memory growth in tests")

    # 5. Reviewability
    if s.diff_lines > LARGE_DIFF_LINES:
        deductions.append(("Size", 5, f"Large change ({s.diff_lines} lines) is harder to review"))

    value = max(0, 100 - sum(p for _, p, _ in deductions))
    band = "High" if value >= HIGH else "Medium" if value >= MEDIUM else "Low"
    return Score(value=value, band=band, deductions=deductions, passed_checks=passed)
