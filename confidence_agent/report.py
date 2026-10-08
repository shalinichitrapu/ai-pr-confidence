"""Render the result as a Markdown PR comment."""

from __future__ import annotations

from typing import Optional

MARKER = "<!-- ai-pr-confidence -->"
ICON = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}


def _lines(nums):
    """Compress [3,4,5,9] into '3-5, 9'."""
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)


def render(score, signals, base_sha: str, head_sha: str, summary: Optional[str]) -> str:
    md = [MARKER, f"## {ICON[score.band]} AI change confidence: **{score.value}/100 ({score.band})**", ""]

    if summary:
        md += ["**Summary** _(written by a local model from the evidence below; it does not affect the score)_", "", summary, ""]

    md += ["| Signal | Impact | Evidence |", "|---|---:|---|"]
    for signal, pts, reason in score.deductions:
        md.append(f"| {signal} | −{pts} | {reason} |")
    for check in score.passed_checks:
        md.append(f"| ✓ | 0 | {check} |")
    md.append("")

    details = []
    if signals.tests_newly_failing:
        details += ["**Newly failing tests**", *[f"- `{t}`" for t in signals.tests_newly_failing], ""]
    if signals.uncovered_changes:
        details += ["**Changed lines no test executes**",
                    *[f"- `{p}` lines {_lines(l)}" for p, l in signals.uncovered_changes.items()], ""]
    if signals.new_error_logs:
        details += ["**New ERROR logs** (signature × count)",
                    *[f"- `{k}` × {v}" for k, v in signals.new_error_logs.items()], ""]
    if signals.new_span_exceptions:
        details += ["**New exceptions on spans**",
                    *[f"- `{k}` × {v}" for k, v in signals.new_span_exceptions.items()], ""]
    if signals.latency_regressions:
        details += ["**Latency regressions** (median span duration)",
                    *[f"- `{k}`: {v['before_ms']} ms → {v['after_ms']} ms" for k, v in signals.latency_regressions.items()], ""]
    if details:
        md += ["<details><summary>Evidence details</summary>", "", *details, "</details>", ""]

    md.append(
        f"<sub>Compared `{head_sha[:7]}` against merge base `{base_sha[:7]}` · "
        f"{signals.tests_total} tests · {signals.diff_lines} lines changed · "
        "score is deterministic: tests, changed-line coverage, logs, traces, latency</sub>"
    )
    return "\n".join(md)
