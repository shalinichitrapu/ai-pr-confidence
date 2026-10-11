"""Turn a baseline run, a PR run and the diff into comparable signals."""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .gitutil import Diff
from .swallow import silent_excepts
from .runner import RunResult

# Thresholds (how much slower, how much a metric or memory must grow) come from
# config.Thresholds. Each requires both a ratio and a real absolute change, so
# noise in tiny values doesn't count.

# Names or attribute values that mark a counter as counting failures.
ERROR_LIKE = re.compile(r"error|fail|invalid|exception|retry|retries|timeout|reject", re.IGNORECASE)


@dataclass
class Signals:
    tests_total: int = 0
    tests_failed: List[str] = field(default_factory=list)
    tests_newly_failing: List[str] = field(default_factory=list)
    tests_removed: List[str] = field(default_factory=list)
    tests_added: List[str] = field(default_factory=list)
    tests_moved: Dict[str, str] = field(default_factory=dict)  # old id -> new id, same test name

    changed_code_lines: int = 0  # executable non-test lines added or modified
    changed_lines_covered: int = 0
    uncovered_changes: Dict[str, List[int]] = field(default_factory=dict)

    new_error_logs: Dict[str, int] = field(default_factory=dict)  # signature -> count
    new_span_exceptions: Dict[str, int] = field(default_factory=dict)
    latency_regressions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Seen only in tests this change added. Often a new test of an error path, so not scored.
    new_test_error_logs: Dict[str, int] = field(default_factory=dict)
    new_test_span_exceptions: Dict[str, int] = field(default_factory=dict)
    # series -> {"test", "kind", "before", "after"} for the test where it grew most
    metric_regressions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    new_error_metrics: Dict[str, float] = field(default_factory=dict)  # series -> total in head
    memory_regressions: Dict[str, Dict[str, float]] = field(default_factory=dict)  # test -> MB before/after
    # kind -> example lines, for changes that make code invisible to the other checks
    hidden_code: Dict[str, List[str]] = field(default_factory=dict)
    # Error signatures matched by [tool.confidence] expected_errors: shown, not scored.
    expected_errors: Dict[str, int] = field(default_factory=dict)
    ignored_files: List[str] = field(default_factory=list)  # matched [tool.confidence] ignore

    diff_lines: int = 0
    code_diff_lines: int = 0  # diff_lines without test files; what the size check uses
    files_changed: List[str] = field(default_factory=list)
    otel_enabled: bool = False
    metrics_enabled: bool = False
    memory_enabled: bool = False
    warnings: List[str] = field(default_factory=list)  # reasons the evidence may be unreliable

    @property
    def changed_line_coverage(self) -> float:
        if self.changed_code_lines == 0:
            return 1.0
        return self.changed_lines_covered / self.changed_code_lines

    def to_dict(self) -> dict:
        d = asdict(self)
        d["changed_line_coverage"] = round(self.changed_line_coverage, 3)
        return d


# Changes that hide code from the agent rather than fix it. Each pattern is
# counted net (removed minus added, or added minus removed for pragmas) across
# the whole diff, so code that merely moved between files is not flagged.
PRAGMA = re.compile(r"#\s*pragma:\s*no\s*cover", re.IGNORECASE)
HIDING_PATTERNS = {
    "coverage exclusions added": (PRAGMA, "added"),
    "error logging removed": (re.compile(r"\blog\w*\.(error|exception|critical)\(", re.IGNORECASE), "removed"),
    "tracing removed": (re.compile(r"\b(start_as_current_span|start_span|record_exception|set_status)\("), "removed"),
    "metric instruments removed": (re.compile(r"\bcreate_(counter|up_down_counter|histogram|gauge)\("), "removed"),
}


def _hidden_code(diff: Diff, tests_path: str) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for kind, (pattern, direction) in HIDING_PATTERNS.items():
        hits = {"added": [], "removed": []}
        for side, texts in (("added", diff.added_text), ("removed", diff.removed_text)):
            for path, lines in texts.items():
                if not path.endswith(".py") or _is_test_file(path, tests_path):
                    continue
                hits[side] += [f"{path}: {line.strip()}" for line in lines if pattern.search(line)]
        other = "removed" if direction == "added" else "added"
        net = len(hits[direction]) - len(hits[other])
        if net > 0:
            found[kind] = hits[direction][:net]
    return found


def _module(path: str) -> str:
    """tests/test_a.py -> tests.test_a, the form JUnit uses for test class names."""
    return path[:-3].replace("/", ".") if path.endswith(".py") else ""


def _renamed_test_id(test_id: str, renamed: Dict[str, str]) -> Optional[str]:
    """The id this test would have after its file's rename, if its file was renamed."""
    classname, _, name = test_id.rpartition("::")
    for new_path, old_path in renamed.items():
        old_mod, new_mod = _module(old_path), _module(new_path)
        if old_mod and (classname == old_mod or classname.startswith(old_mod + ".")):
            return f"{new_mod}{classname[len(old_mod):]}::{name}"
    return None


def _is_test_file(path: str, tests_path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(tests_path.rstrip("/") + "/") or name.startswith("test_") or name == "conftest.py"


def _in_source(path: str, source: str) -> bool:
    """True if `path` is inside a --source folder or package coverage measured.

    Like coverage.py, `source` may list several, separated by commas.
    """
    for src in source.split(","):
        src = src.strip().rstrip("/")
        if src.startswith("./"):
            src = src[2:]
        if src in ("", "."):
            return True
        if "/" not in src:
            src = src.replace(".", "/")  # dotted package name, e.g. my.pkg
        # A folder, or a single module: coverage's --source=app also means app.py.
        if path == src or path == src + ".py" or path.startswith(src + "/"):
            return True
    return False


Keep = Callable[[Optional[str]], bool]


def _all(test: Optional[str]) -> bool:
    return True


def _error_log_signatures(run: RunResult, keep: Keep = _all) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rec in run.logs:
        if rec["level"] not in ("ERROR", "CRITICAL") or not keep(rec["test"]):
            continue
        sig = f"{rec['logger']} {rec['location']}: {rec['exc_type'] or rec['template']}"
        counts[sig] = counts.get(sig, 0) + 1
    return counts


def _span_exception_signatures(run: RunResult, keep: Keep = _all) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for span in run.spans:
        if not keep(span["test"]):
            continue
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


def _median_durations(run: RunResult, keep: Keep = _all) -> Dict[str, float]:
    by_name: Dict[str, List[float]] = {}
    for span in run.spans:
        if not keep(span["test"]):
            continue
        by_name.setdefault(span["name"], []).append(span["duration_ms"])
    return {name: statistics.median(v) for name, v in by_name.items()}


def _without_ignored(diff: Diff, cfg: Config) -> Tuple[Diff, List[str]]:
    """A copy of the diff without files matching [tool.confidence] ignore."""
    ignored = sorted(p for p in diff.files_changed if cfg.ignored(p))
    if not ignored:
        return diff, []
    keep = lambda d: {p: v for p, v in d.items() if not cfg.ignored(p)}  # noqa: E731
    kept = Diff(
        added_lines=keep(diff.added_lines),
        files_changed=[p for p in diff.files_changed if not cfg.ignored(p)],
        file_lines=keep(diff.file_lines),
        renamed=keep(diff.renamed),
        lines_added=diff.lines_added,
        lines_removed=diff.lines_removed,
        added_text=keep(diff.added_text),
        removed_text=keep(diff.removed_text),
        head_source=keep(diff.head_source),
    )
    return kept, ignored


def compute(base: RunResult, head: RunResult, diff: Diff, tests_path: str, source: str = ".",
            config: Optional[Config] = None) -> Signals:
    cfg = config or Config()
    th = cfg.thresholds
    s = Signals(otel_enabled=head.otel_enabled)
    diff, s.ignored_files = _without_ignored(diff, cfg)
    s.hidden_code = _hidden_code(diff, tests_path)
    silenced = [
        f"{path}:{line}: {diff.head_source[path].splitlines()[line - 1].strip()}"
        for path, text in diff.head_source.items()
        if not _is_test_file(path, tests_path)
        for line in silent_excepts(text, diff.added_lines.get(path, ()))
    ]
    if silenced:
        s.hidden_code["errors silenced without a trace"] = silenced

    s.tests_total = len(head.tests)
    s.tests_failed = head.failed
    base_failed = set(base.failed)
    s.tests_newly_failing = [t for t in head.failed if t not in base_failed]
    removed = sorted(set(base.tests) - set(head.tests))
    s.tests_added = sorted(set(head.tests) - set(base.tests))
    # A test counts as moved only when git saw its file renamed and the test kept
    # its class and name. Matching on the name alone would let a change delete a
    # failing test and add a weaker one with the same name somewhere else.
    added = set(s.tests_added)
    for old in removed:
        new = _renamed_test_id(old, diff.renamed)
        if new in added:
            s.tests_moved[old] = new
            added.discard(new)
        else:
            s.tests_removed.append(old)

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
        if any(PRAGMA.search(line) for line in diff.added_text.get(path, [])):
            # This change added a coverage exclusion here, so lines it excluded
            # still count as changed code: hiding a line must not raise the score.
            executable |= set(cov.get("excluded_lines", []))
        changed = added & executable
        covered = changed & executed
        s.changed_code_lines += len(changed)
        s.changed_lines_covered += len(covered)
        if changed - covered:
            s.uncovered_changes[path] = sorted(changed - covered)

    # Runtime evidence is scored only for tests that exist before and after, so
    # both runs saw the same inputs. Records outside any test (e.g. at import)
    # count too. Adding a test of an error path must not cost points.
    shared = set(base.node_ids) & set(head.node_ids)

    def in_shared(test: Optional[str]) -> bool:
        return test is None or test in shared

    def in_new(test: Optional[str]) -> bool:
        return test is not None and test not in shared

    base_logs = _error_log_signatures(base)
    s.new_error_logs = {k: v for k, v in _error_log_signatures(head, in_shared).items() if k not in base_logs}
    s.new_test_error_logs = {k: v for k, v in _error_log_signatures(head, in_new).items() if k not in base_logs}
    base_exc = _span_exception_signatures(base)
    s.new_span_exceptions = {
        k: v for k, v in _span_exception_signatures(head, in_shared).items() if k not in base_exc
    }
    s.new_test_span_exceptions = {
        k: v for k, v in _span_exception_signatures(head, in_new).items() if k not in base_exc
    }

    for found in (s.new_error_logs, s.new_span_exceptions):
        for sig in [k for k in found if cfg.expected(k)]:
            s.expected_errors[sig] = s.expected_errors.get(sig, 0) + found.pop(sig)

    base_lat, head_lat = _median_durations(base, in_shared), _median_durations(head, in_shared)
    for name, after in head_lat.items():
        before = base_lat.get(name)
        if before is None:
            continue
        if after >= before * th.latency_ratio and after - before >= th.latency_min_delta_ms:
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
        if after >= before * th.metric_ratio and growth >= th.metric_min_delta:
            worst = s.metric_regressions.get(series)
            if worst is None or growth > worst["after"] - worst["before"]:
                s.metric_regressions[series] = {"test": test, "kind": kind, "before": before, "after": after}

    s.memory_enabled = bool(base.memory and head.memory)
    for test, after in head.memory.items():
        before = base.memory.get(test)
        if before is None:
            continue
        if after >= before * th.memory_ratio and after - before >= th.memory_min_delta_mb * 2**20:
            s.memory_regressions[test] = {
                "before_mb": round(before / 2**20, 1), "after_mb": round(after / 2**20, 1)
            }

    for run in (base, head):
        if run.outside_imports:
            shown = ", ".join(f"`{p}`" for p in run.outside_imports[:3])
            more = f" and {len(run.outside_imports) - 3} more" if len(run.outside_imports) > 3 else ""
            s.warnings.append(
                f"The {run.label} run imported {shown}{more} from your working copy instead of the "
                f"{run.label} checkout, so it may not have tested the {run.label} version. "
                "Check how the project is installed (e.g. `pip install -e`) and the test import path."
            )

    s.diff_lines = diff.size
    s.code_diff_lines = sum(n for path, n in diff.file_lines.items() if not _is_test_file(path, tests_path))
    s.files_changed = diff.files_changed
    return s
