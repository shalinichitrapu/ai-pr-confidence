"""Optional plain-English summary from a local model served by Ollama.

Uses only the standard library. If the model server is unreachable or slow,
the agent carries on without a summary; the score never depends on this.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

PROMPT = """You review pull requests. A tool has already measured this change and \
computed a confidence score. Do NOT change or second-guess the score.

Write 2-4 short bullet points for the reviewer: the biggest risk first, what to \
check before merging, and anything reassuring. Use only the evidence below. \
Plain text bullets starting with "- ", no preamble.

Score: {score}/100 ({band})
Deductions: {deductions}
Passed checks: {passed}
Evidence: {evidence}
Changed files: {files}
"""


def summarize(url: str, model: str, score, signals, timeout: int = 240) -> Optional[str]:
    evidence = {
        "newly_failing_tests": signals.tests_newly_failing,
        "uncovered_changed_lines": signals.uncovered_changes,
        "new_error_logs": signals.new_error_logs,
        "new_span_exceptions": signals.new_span_exceptions,
        "latency_regressions": signals.latency_regressions,
        "metric_regressions": signals.metric_regressions,
        "new_error_metrics": signals.new_error_metrics,
        "memory_regressions": signals.memory_regressions,
    }
    prompt = PROMPT.format(
        score=score.value,
        band=score.band,
        deductions=[f"-{p} {reason}" for _, p, reason in score.deductions] or "none",
        passed=score.passed_checks,
        evidence=json.dumps(evidence),
        files=signals.files_changed,
    )
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}}
    ).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = json.loads(resp.read()).get("response", "").strip()
            return text or None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[confidence] LLM summary skipped: {exc}")
        return None
