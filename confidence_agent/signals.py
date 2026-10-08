"""Turn a baseline run, a PR run and the diff into comparable signals."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Dict, List

from .gitutil import Diff
from .runner import RunResult

# A span is "slower" only if it is both much slower and slower by a real amount,
# so tiny timing noise in fast functions does not count.
LATENCY_RATIO = 2.0
LATENCY_MIN_DELTA_MS = 5.0


@dataclass
class Signals:
    tests_total: int = 0
    tests_failed: List[str] = field(default_factory=list)
    tests_newly_failing: List[str] = field(default_factory=list)
    tests_removed: List[str] = field(default_factory=list)
    tests_added: List[str] = field(default_factory=list)

    changed_code_lines: int = 0  # executable non-test lines added or modified
    changed_lines_covered: int = 0
    uncovered_changes: Dict[str, List[int]] = field(default_factory=dict)

    new_error_logs: Dict[str, int] = field(default_factory=dict)  # signature -> count
    new_span_exceptions: Dict[str, int] = field(default_factory=dict)
    latency_regressions: Dict[str, Dict[str, float]] = field(default_factory=dict)

    diff_lines: int = 0
    files_changed: List[str] = field(default_factory=list)
    otel_enabled: bool = False

    @property
    def changed_line_coverage(self) -> float:
        if self.changed_code_lines == 0:
            return 1.0
        return self.changed_lines_covered / self.changed_code_lines

    def to_dict(self) -> dict:
        d = asdict(self)
        d["changed_line_coverage"] = round(self.changed_line_coverage, 3)
        return d


def _is_test_file(path: str, tests_path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(tests_path.rstrip("/") + "/") or name.startswith("test_") or name == "conftest.py"


def _error_log_signatures(run: RunResult) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rec in run.logs:
        if rec["level"] not in ("ERROR", "CRITICAL"):
            continue
        sig = f"{rec['logger']} {rec['location']}: {rec['exc_type'] or rec['template']}"
        counts[sig] = counts.get(sig, 0) + 1
    return counts


def _span_exception_signatures(run: RunResult) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for span in run.spans:
        for ev in span["events"]:
            if ev["name"] == "exception":
                sig = f"{span['name']}: {ev['exc_type']}"
                counts[sig] = counts.get(sig, 0) + 1
    return counts


def _median_durations(run: RunResult) -> Dict[str, float]:
    by_name: Dict[str, List[float]] = {}
    for span in run.spans:
        by_name.setdefault(span["name"], []).append(span["duration_ms"])
    return {name: statistics.median(v) for name, v in by_name.items()}


def compute(base: RunResult, head: RunResult, diff: Diff, tests_path: str) -> Signals:
    s = Signals(otel_enabled=head.otel_enabled)

    s.tests_total = len(head.tests)
    s.tests_failed = head.failed
    base_failed = set(base.failed)
    s.tests_newly_failing = [t for t in head.failed if t not in base_failed]
    s.tests_removed = sorted(set(base.tests) - set(head.tests))
    s.tests_added = sorted(set(head.tests) - set(base.tests))

    for path, added in diff.added_lines.items():
        if not path.endswith(".py") or _is_test_file(path, tests_path):
            continue
        cov = head.coverage.get(path)
        if cov is None:
            # coverage.py never saw the file: no test imported it at all.
            s.changed_code_lines += len(added)
            s.uncovered_changes[path] = sorted(added)
            continue
        executed = set(cov.get("executed_lines", []))
        executable = executed | set(cov.get("missing_lines", []))
        changed = added & executable
        covered = changed & executed
        s.changed_code_lines += len(changed)
        s.changed_lines_covered += len(covered)
        if changed - covered:
            s.uncovered_changes[path] = sorted(changed - covered)

    base_logs, head_logs = _error_log_signatures(base), _error_log_signatures(head)
    s.new_error_logs = {k: v for k, v in head_logs.items() if k not in base_logs}
    base_exc, head_exc = _span_exception_signatures(base), _span_exception_signatures(head)
    s.new_span_exceptions = {k: v for k, v in head_exc.items() if k not in base_exc}

    base_lat, head_lat = _median_durations(base), _median_durations(head)
    for name, after in head_lat.items():
        before = base_lat.get(name)
        if before is None:
            continue
        if after >= before * LATENCY_RATIO and after - before >= LATENCY_MIN_DELTA_MS:
            s.latency_regressions[name] = {"before_ms": round(before, 2), "after_ms": round(after, 2)}

    s.diff_lines = diff.size
    s.files_changed = diff.files_changed
    return s
