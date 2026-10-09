"""Turn a baseline run, a PR run and the diff into comparable signals."""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Tuple

from .gitutil import Diff
from .runner import RunResult

# A span is "slower" only if it is both much slower and slower by a real amount,
# so tiny timing noise in fast functions does not count.
LATENCY_RATIO = 2.0
LATENCY_MIN_DELTA_MS = 5.0

# Metric values from the same test with the same inputs should not move much.
# Flag a series only if it at least doubled and grew by a real amount.
METRIC_RATIO = 2.0
METRIC_MIN_DELTA = 3.0
# Names or attribute values that mark a counter as counting failures.
ERROR_LIKE = re.compile(r"error|fail|invalid|exception|retry|timeout|reject", re.IGNORECASE)

# A test's peak memory must both double and grow by at least this much.
MEMORY_RATIO = 2.0
MEMORY_MIN_DELTA_BYTES = 5 * 1024 * 1024


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
    # series -> {"test", "kind", "before", "after"} for the test where it grew most
    metric_regressions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    new_error_metrics: Dict[str, float] = field(default_factory=dict)  # series -> total in head
    memory_regressions: Dict[str, Dict[str, float]] = field(default_factory=dict)  # test -> MB before/after

    diff_lines: int = 0
    files_changed: List[str] = field(default_factory=list)
    otel_enabled: bool = False
    metrics_enabled: bool = False
    memory_enabled: bool = False

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


def _in_source(path: str, source: str) -> bool:
    """True if `path` is inside the --source folder or package coverage measured."""
    src = source.strip().rstrip("/")
    if src.startswith("./"):
        src = src[2:]
    if src in ("", "."):
        return True
    if "/" not in src:
        src = src.replace(".", "/")  # dotted package name, e.g. my.pkg
    return path == src or path.startswith(src + "/")


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


def _series(point: Dict[str, Any]) -> str:
    attrs = ",".join(f"{k}={v}" for k, v in sorted(point["attributes"].items()))
    return f"{point['name']}{{{attrs}}}" if attrs else point["name"]


def _metric_values(run: RunResult) -> Dict[Tuple[str, str], Tuple[str, float]]:
    """(test, series) -> (kind, value) where value is what one test recorded.

    Counters and up-down counters: the amount added during the test.
    Gauges: the last value set. Histograms: the mean of recorded values.
    """
    out: Dict[Tuple[str, str], Tuple[str, float]] = {}
    hist: Dict[Tuple[str, str], List[float]] = {}
    for p in run.metrics:
        key = (p["test"], _series(p))
        if p["kind"] == "histogram":
            c, s = hist.get(key, [0, 0.0])
            hist[key] = [c + p["count"], s + p["sum"]]
        elif p["kind"] == "gauge":
            out[key] = ("gauge", p["value"])
        else:
            prev = out.get(key, (p["kind"], 0))[1]
            out[key] = (p["kind"], prev + p["value"])
    for key, (count, total) in hist.items():
        if count:
            out[key] = ("histogram mean", total / count)
    return out


def _median_durations(run: RunResult) -> Dict[str, float]:
    by_name: Dict[str, List[float]] = {}
    for span in run.spans:
        by_name.setdefault(span["name"], []).append(span["duration_ms"])
    return {name: statistics.median(v) for name, v in by_name.items()}


def compute(base: RunResult, head: RunResult, diff: Diff, tests_path: str, source: str = ".") -> Signals:
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
        if not _in_source(path, source):
            # Coverage was never measured here, so it can't say whether tests run it.
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

    # Metrics: compare each series within the same test, which ran the same inputs.
    s.metrics_enabled = head.otel_metrics_enabled and bool(base.metrics or head.metrics)
    base_m, head_m = _metric_values(base), _metric_values(head)
    shared_tests = set(base.node_ids) & set(head.node_ids)
    for (test, series), (kind, after) in head_m.items():
        if test not in shared_tests:
            continue
        before = base_m.get((test, series), (kind, None))[1]
        if before is None:
            if kind == "counter" and after > 0 and ERROR_LIKE.search(series):
                s.new_error_metrics[series] = s.new_error_metrics.get(series, 0) + after
            continue
        growth = after - before
        if after >= before * METRIC_RATIO and growth >= METRIC_MIN_DELTA:
            worst = s.metric_regressions.get(series)
            if worst is None or growth > worst["after"] - worst["before"]:
                s.metric_regressions[series] = {"test": test, "kind": kind, "before": before, "after": after}

    s.memory_enabled = bool(base.memory and head.memory)
    for test, after in head.memory.items():
        before = base.memory.get(test)
        if before is None:
            continue
        if after >= before * MEMORY_RATIO and after - before >= MEMORY_MIN_DELTA_BYTES:
            s.memory_regressions[test] = {
                "before_mb": round(before / 2**20, 1), "after_mb": round(after / 2**20, 1)
            }

    s.diff_lines = diff.size
    s.files_changed = diff.files_changed
    return s
