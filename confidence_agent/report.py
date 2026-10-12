"""Render the result as a Markdown PR comment."""

from __future__ import annotations

from typing import Optional, Sequence

MARKER = "<!-- ai-pr-confidence -->"
ICON = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}


def _code(value) -> str:
    """Text the pull request controls (test names, log messages, paths), shown as inline code.

    Backticks and line breaks are neutralized so the value can't close the code
    span and inject Markdown (fake rows, headings, links) into the comment.
    """
    text = " ".join(str(value).replace("`", "ˋ").split())
    return f"`{text}`"


def _cell(text) -> str:
    """Plain text safe inside a table cell: one line, no pipes or HTML."""
    return " ".join(str(text).split()).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _line(text) -> str:
    """Plain text safe as one Markdown line (warnings, notes)."""
    return " ".join(str(text).split()).replace("<", "&lt;")


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


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def render(score, signals, base_sha: str, head_sha: str, summary: Optional[str], notes: Sequence[str] = ()) -> str:
    md = [MARKER, f"## {ICON[score.band]} AI change confidence: **{score.value}/100 ({score.band})**", ""]

    for warning in signals.warnings:
        md += [f"> ⚠️ **Results may be unreliable.** {_line(warning)}", ""]

    md += ["| Signal | Impact | Evidence |", "|---|---:|---|"]
    keys = getattr(score, "keys", []) or [""] * len(score.deductions)
    for (signal, pts, reason), key in zip(score.deductions, keys):
        hint = f" <sub>(ack: `confidence-ack:{key}`)</sub>" if key and key not in ("tests-failing", "no-tests") else ""
        md.append(f"| {signal} | −{pts} | {_cell(reason)}{hint} |")
    for signal, pts, reason, who in score.acknowledged:
        md.append(f"| {signal} | 0 (−{pts} acknowledged by {_code(who)}) | {_cell(reason)} |")
    for check in score.passed_checks:
        md.append(f"| ✓ | 0 | {check} |")
    md.append("")
    if any(k not in ("tests-failing", "no-tests") for k in keys if k):
        md += ["<sub>If a deduction is expected, a reviewer can acknowledge it by adding the label shown on "
               "its row.</sub>", ""]
    for note in notes:
        md += [f"_{_line(note)}_", ""]

    # Below the measured evidence, not above it: the model reads text the pull
    # request controls, so its summary should never be the first thing a reviewer sees.
    if summary:
        md += ["**Model summary** _(a local model's reading of the evidence above. It can be wrong "
               "and does not affect the score.)_", "", summary, ""]

    details = []
    if signals.tests_newly_failing:
        details += ["**Newly failing tests**", *[f"- {_code(t)}" for t in signals.tests_newly_failing], ""]
    if signals.tests_failed and not signals.tests_newly_failing:
        details += ["**Tests that failed before and after this change**",
                    *[f"- {_code(t)}" for t in signals.tests_failed], ""]
    if signals.tests_flaky:
        details += ["**Flaky tests** (failed, then passed when run again)",
                    *[f"- {_code(t)}" for t in signals.tests_flaky], ""]
    if signals.tests_moved:
        details += ["**Tests moved, not removed** (git detected the file rename)",
                    *[f"- {_code(a)} → {_code(b)}" for a, b in signals.tests_moved.items()], ""]
    if signals.tests_removed:
        details += ["**Tests removed by this change**", *[f"- {_code(t)}" for t in signals.tests_removed], ""]
    if signals.uncovered_changes:
        details += ["**Changed lines no test executes**",
                    *[f"- {_code(p)} lines {_lines(l)}" for p, l in signals.uncovered_changes.items()], ""]
    if signals.latency_skipped:
        details += [f"**Latency not compared:** {signals.latency_skipped}. Run with `--no-cache` to compare it.", ""]
    if signals.mutation_note:
        details += [f"**Mutation testing** {signals.mutation_note}.", ""]
    if signals.mutants_survived:
        details += ["**Mutations no test noticed** (the line was changed like this and every covering test still passed)",
                    *[f"- {_code(m)}" for m in signals.mutants_survived[:10]], ""]
    if signals.new_error_logs:
        details += ["**New ERROR logs** (signature × count)",
                    *[f"- {_code(k)} × {v}" for k, v in signals.new_error_logs.items()], ""]
    if signals.new_span_exceptions:
        details += ["**New exceptions on spans**",
                    *[f"- {_code(k)} × {v}" for k, v in signals.new_span_exceptions.items()], ""]
    if signals.new_test_error_logs or signals.new_test_span_exceptions:
        details += ["**Errors seen only in tests this change adds** (half weight; check they're expected)",
                    *[f"- log {_code(k)} × {v}" for k, v in signals.new_test_error_logs.items()],
                    *[f"- span {_code(k)} × {v}" for k, v in signals.new_test_span_exceptions.items()], ""]
    if signals.new_test_expected_errors:
        details += ["**Errors in new tests that expect an exception** (`pytest.raises` / `assertRaises`; not scored)",
                    *[f"- {_code(k)} × {v}" for k, v in signals.new_test_expected_errors.items()], ""]
    if signals.latency_regressions:
        details += ["**Latency regressions** (median span duration)",
                    *[f"- {_code(k)}: {v['before_ms']} ms → {v['after_ms']} ms" for k, v in signals.latency_regressions.items()], ""]
    if signals.metric_regressions:
        details += ["**Metrics that grew** (same test, before → after)",
                    *[f"- {_code(k)} ({v['kind']}) in {_code(v['test'])}: {_num(v['before'])} → {_num(v['after'])}"
                      for k, v in signals.metric_regressions.items()], ""]
    if signals.new_error_metrics:
        details += ["**New error counters**",
                    *[f"- {_code(k)} × {_num(v)}" for k, v in signals.new_error_metrics.items()], ""]
    if signals.memory_regressions:
        details += ["**Peak memory per test**",
                    *[f"- {_code(k)}: {v['before_mb']} MB → {v['after_mb']} MB" for k, v in signals.memory_regressions.items()], ""]
    if signals.hidden_code:
        details += ["**Code hidden from the other checks** (net, across the whole diff)"]
        for kind, lines in signals.hidden_code.items():
            details += [f"- {kind}:", *[f"  - {_code(line)}" for line in lines[:5]]]
        details.append("")
    if signals.risky_code:
        details += ["**Security-sensitive code on changed lines**", *[f"- {_code(f)}" for f in signals.risky_code], ""]
    if signals.dependency_changes:
        details += ["**Dependencies added or changed**", *[f"- {_code(c)}" for c in signals.dependency_changes], ""]
    if signals.unmeasured_files:
        details += ["**Changed files the tests can't exercise** (configuration, CI, infrastructure; review directly)",
                    *[f"- {_code(p)}" for p in signals.unmeasured_files], ""]
    if signals.expected_errors:
        details += ["**Expected errors** (listed in `[tool.confidence] expected_errors`, not scored)",
                    *[f"- {_code(k)} × {v}" for k, v in signals.expected_errors.items()], ""]
    if signals.ignored_files:
        details += ["**Files not scored** (matched `[tool.confidence] ignore`)",
                    *[f"- {_code(p)}" for p in signals.ignored_files], ""]
    if details:
        md += ["<details><summary>Evidence details</summary>", "", *details, "</details>", ""]

    md.append(
        f"<sub>Compared {_code(head_sha[:7])} against merge base {_code(base_sha[:7])} · "
        f"{signals.tests_total} tests · {signals.diff_lines} lines changed · "
        "scored from tests, changed-line coverage, logs, traces, metrics, latency and memory, not by a model; "
        "latency and memory are single-run measurements</sub>"
    )
    return "\n".join(md)
