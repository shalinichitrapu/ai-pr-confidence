"""Turn a baseline run, a PR run and the diff into comparable signals."""

from __future__ import annotations

import ast
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config
from .expectations import expects_exception, node_file
from .gitutil import Diff
from .review_scope import config_only, risky_code, unmeasured
from .swallow import new_silent_excepts, removed_silent_handlers
from .runner import RunResult, junit_id

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
    mutants_tested: int = 0  # mutants that ran (killed + survived)
    mutants_survived: List[str] = field(default_factory=list)  # changes no covering test noticed
    dependency_changes: List[str] = field(default_factory=list)  # added or changed packages
    risky_code: List[str] = field(default_factory=list)  # security-sensitive calls on changed lines
    unmeasured_files: List[str] = field(default_factory=list)  # config/data files tests can't exercise
    mutation_note: str = ""  # why mutation testing didn't run or couldn't conclude
    tests_skipped_count: int = 0  # every skipped/xfailed test in the head run
    tests_skipped: List[str] = field(default_factory=list)  # passed before, skipped or xfailed now
    tests_flaky: List[str] = field(default_factory=list)  # newly failed, then passed on a rerun
    tests_moved: Dict[str, str] = field(default_factory=dict)  # old id -> new id, same test name

    changed_code_lines: int = 0  # executable non-test lines added or modified
    changed_lines_covered: int = 0
    uncovered_changes: Dict[str, List[int]] = field(default_factory=dict)

    new_error_logs: Dict[str, int] = field(default_factory=dict)  # signature -> count
    new_span_exceptions: Dict[str, int] = field(default_factory=dict)
    latency_regressions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    latency_skipped: str = ""  # why latency wasn't compared
    # Seen only in tests this change added. Scored at half weight, because a new
    # feature's own tests are the only place its swallowed errors can show up...
    new_test_error_logs: Dict[str, int] = field(default_factory=dict)
    new_test_span_exceptions: Dict[str, int] = field(default_factory=dict)
    # ...unless the test expects an exception (pytest.raises / assertRaises): not scored.
    new_test_expected_errors: Dict[str, int] = field(default_factory=dict)
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
ERROR_LOG_CALL = re.compile(r"(?:\b|_)log\w*\.(error|exception|critical)\(|getLogger\([^)]*\)\.(error|exception|critical)\(",
                            re.IGNORECASE)
HIDING_PATTERNS = {
    "coverage exclusions added": (PRAGMA, "added", "code"),
    "error logging removed": (ERROR_LOG_CALL, "removed", "code"),
    "tracing removed": (re.compile(r"\b(start_as_current_span|start_span|record_exception)\(|"
                                   r"\bset_status\(.*ERROR"), "removed", "code"),
    "metric instruments removed": (re.compile(r"\bcreate_(counter|up_down_counter|histogram|gauge)\("), "removed", "code"),
    # An assertion that compared values, replaced by a weaker one or none.
    # (`is None` / `is not None` checks don't pin a value, so they don't count as comparisons.)
    "test assertions weakened": (re.compile(r"^\s*(assert\b.*(==|!=|<=|>=|<|>|\bin\b)|self\.assert(Equal|In|Raises|Less|Greater|AlmostEqual))"),
                                 "removed", "tests"),
}
COLLECTION_TRICKS = re.compile(r"collect_ignore|pytest_collection_modifyitems|pytest\.skip\(|pytest\.mark\.(skip|xfail)"
                               r"|deselect|pytest_ignore_collect")


def _deleted_definition_lines(diff: Diff) -> Dict[str, set]:
    """Base lines of functions, classes and files this change deleted entirely.

    Removing instrumentation together with the code it instrumented is dead-code
    removal, not hiding.
    """
    out: Dict[str, set] = {}
    for path, removed in diff.removed_lines.items():
        if path not in diff.added_lines and path not in diff.renamed.values():
            out[path] = set(removed)  # the whole file was deleted
            continue
        source = diff.base_source.get(path)
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                span = set(range(first, (node.end_lineno or node.lineno) + 1))
                if span <= removed:
                    out.setdefault(path, set()).update(span)
    return out


def _removed_with_numbers(diff: Diff, path: str):
    """(base line number, text) for each removed line, when they line up."""
    numbers, texts = sorted(diff.removed_lines.get(path, ())), diff.removed_text.get(path, [])
    return list(zip(numbers, texts)) if len(numbers) == len(texts) else [(None, t) for t in texts]


def _hidden_code(diff: Diff, tests_path: str) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    deleted = _deleted_definition_lines(diff)
    for kind, (pattern, direction, where) in HIDING_PATTERNS.items():
        hits = {"added": [], "removed": []}

        def wanted(path):
            return path.endswith(".py") and (_is_test_file(path, tests_path) == (where == "tests"))

        for path, lines in diff.added_text.items():
            if wanted(path):
                hits["added"] += [f"{path}: {line.strip()}" for line in lines if pattern.search(line)]
        for path in diff.removed_text:
            if wanted(path):
                gone = deleted.get(path, set())
                hits["removed"] += [f"{path}: {text.strip()}" for n, text in _removed_with_numbers(diff, path)
                                    if pattern.search(text) and n not in gone]
        other = "removed" if direction == "added" else "added"
        net = len(hits[direction]) - len(hits[other])
        if net > 0:
            found[kind] = hits[direction][:net]
    tricks = [f"{path}: {line.strip()}" for path, lines in diff.added_text.items()
              if path.rsplit("/", 1)[-1] == "conftest.py" for line in lines if COLLECTION_TRICKS.search(line)]
    tricks += [f"pytest settings changed in {c}" for c in diff.pytest_config_changes]
    if tricks:
        found["test collection or configuration changed"] = tricks
    return found


def _drops_error_handling(diff: Diff, path: str) -> bool:
    """Did this change remove more `raise` / error-logging lines than it added in this file?"""
    def count(lines):
        return sum(1 for line in lines if re.match(r"\s*raise\b", line) or ERROR_LOG_CALL.search(line))
    return count(diff.removed_text.get(path, [])) > count(diff.added_text.get(path, []))


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


def mutation_targets(diff: Diff, coverage: Dict[str, Any], tests_path: str, source: str,
                     cfg: Optional[Config] = None) -> Dict[str, List[int]]:
    """Changed, executed lines of source files: the lines worth mutating."""
    cfg = cfg or Config()
    targets = {}
    for path, added in diff.added_lines.items():
        if (not path.endswith(".py") or _is_test_file(path, tests_path) or not _in_source(path, source)
                or cfg.ignored(path) or path not in coverage):
            continue
        lines = sorted(added & set(coverage[path].get("executed_lines", [])))
        if lines:
            targets[path] = lines
    return targets


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


def _is_error_log(rec: Dict[str, Any]) -> bool:
    return rec["level"] in ("ERROR", "CRITICAL")


def _log_signature(rec: Dict[str, Any]) -> str:
    return f"{rec['logger']} {rec['location']}: {rec['exc_type'] or rec['template']}"


def _record_signatures(records, signature, keep_record):
    """(test, signature) for each error log (signature given) or each exception on a span (None)."""
    for rec in records:
        if signature is not None:
            if keep_record(rec):
                yield rec["test"], signature(rec)
            continue
        for ev in rec["events"]:
            if ev["name"] == "exception":
                yield rec["test"], f"{rec['name']}: {ev['exc_type']}"


def _signatures_by_test(records, signature, keep_record) -> Dict[Optional[str], set]:
    found: Dict[Optional[str], set] = {}
    for test, sig in _record_signatures(records, signature, keep_record):
        found.setdefault(test, set()).add(sig)
    return found


def _error_like(point: Dict[str, Any]) -> bool:
    """A counter of failures: by its name or an attribute *value* (not the attribute's name)."""
    return bool(ERROR_LIKE.search(point["name"]) or any(ERROR_LIKE.search(str(v)) for v in point["attributes"].values()))


DOCS_AND_LOCKFILES = ("*.md", "*.rst", "*.txt", "docs/*", "*/docs/*", "LICENSE*", "CHANGELOG*", "*.lock",
                      "*-lock.json", "*.lock.json", "poetry.lock", "uv.lock", "Pipfile.lock")


def _is_docs_or_lockfile(path: str) -> bool:
    from fnmatch import fnmatch
    from .review_scope import is_dependency_file
    return not is_dependency_file(path) and any(fnmatch(path, p) for p in DOCS_AND_LOCKFILES)


def _statement_lines(source: str) -> set:
    """Lines that start a statement: what coverage would count as executable (roughly)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {node.lineno for node in ast.walk(tree) if isinstance(node, ast.stmt)}


def _moved_lines(diff: Diff, tests_path: str) -> Dict[str, set]:
    """Added lines that are the same as lines this change removed elsewhere: moved code, not new code.

    Matched by their text without indentation, one removed line per added line.
    Short or trivial lines (`pass`, `return None`, `else:`) aren't matched, since
    they'd pair up by chance.
    """
    def meaningful(text: str) -> bool:
        return len(text) >= 12 and not text.startswith("#")

    removed: Dict[str, int] = {}
    for path, lines in diff.removed_text.items():
        if path.endswith(".py") and not _is_test_file(path, tests_path):
            for line in lines:
                text = line.strip()
                if meaningful(text):
                    removed[text] = removed.get(text, 0) + 1
    moved: Dict[str, set] = {}
    for path, numbers in diff.added_lines.items():
        source = diff.head_source.get(path)
        if source is None or _is_test_file(path, tests_path):
            continue
        lines = source.split("\n")
        for n in sorted(numbers):
            text = lines[n - 1].strip() if n <= len(lines) else ""
            if meaningful(text) and removed.get(text, 0) > 0:
                removed[text] -= 1
                moved.setdefault(path, set()).add(n)
    return moved


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


def _median_durations(run: RunResult, keep: Keep) -> Dict[str, float]:
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
        pytest_config_changes=list(diff.pytest_config_changes),
        dependency_changes=[c for c in diff.dependency_changes
                            if not cfg.ignored(c.split(":", 1)[0].split(" (", 1)[0])],
        removed_lines=keep(diff.removed_lines),
        base_source=keep(diff.base_source),
    )
    return kept, ignored


def compute(base: RunResult, head: RunResult, diff: Diff, tests_path: str, source: str = ".",
            config: Optional[Config] = None) -> Signals:
    cfg = config or Config()
    th = cfg.thresholds
    s = Signals(otel_enabled=head.otel_enabled)
    diff, s.ignored_files = _without_ignored(diff, cfg)
    if head.mutation is not None:
        s.mutants_tested = head.mutation.killed + len(head.mutation.survived)
        s.mutants_survived = list(head.mutation.survived)
        s.mutation_note = head.mutation.note
    s.hidden_code = _hidden_code(diff, tests_path)
    removed = removed_silent_handlers(diff.base_source, diff.removed_lines,
                                      skip=lambda path: _is_test_file(path, tests_path))
    silenced, in_tests = [], []
    for path, text in sorted(diff.head_source.items()):
        changed = diff.added_lines.get(path, ())
        if _is_test_file(path, tests_path):
            in_tests += new_silent_excepts(path, text, changed, Counter(), in_test=True)
        else:
            silenced += new_silent_excepts(path, text, changed, removed,
                                           narrow_too=_drops_error_handling(diff, path))
    if silenced:
        s.hidden_code["errors silenced without a trace"] = silenced
    if in_tests:
        s.hidden_code["test failures silenced"] = in_tests

    s.dependency_changes = list(diff.dependency_changes)
    s.risky_code = [
        finding
        for path, text in diff.head_source.items()
        if not _is_test_file(path, tests_path)
        for finding in risky_code(path, text, diff.added_lines.get(path, ()))
    ]
    s.unmeasured_files = unmeasured(diff.files_changed)
    if config_only(diff.files_changed):
        s.warnings.append(
            "This change only touches files the tests can't exercise (configuration, CI or "
            "infrastructure), so the score says little about it. Review those files directly."
        )

    s.tests_total = len(head.tests)
    s.tests_skipped_count = sum(1 for o in head.tests.values() if o == "skipped")
    # A test that passed before and is skipped (or xfailed) now hides its result as
    # surely as deleting it.
    s.tests_skipped = sorted(t for t, o in head.tests.items() if o == "skipped" and base.tests.get(t) == "passed")
    base_failed = set(base.failed)
    s.tests_flaky = [t for t in head.failed if t not in base_failed and head.rerun.get(t) == "passed"]
    s.tests_failed = [t for t in head.failed if t not in s.tests_flaky]
    s.tests_newly_failing = [t for t in s.tests_failed if t not in base_failed]
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

    moved = _moved_lines(diff, tests_path)
    for path, added in diff.added_lines.items():
        if not path.endswith(".py") or _is_test_file(path, tests_path):
            continue
        if not _in_source(path, source):
            # Coverage was never measured here, so it can't say whether tests run it.
            continue
        added = added - moved.get(path, set())
        cov = head.coverage.get(path)
        if cov is None:
            # coverage.py never saw the file: no test imported it at all. Count its
            # statements, not its comments and blank lines.
            statements = _statement_lines(diff.head_source.get(path, ""))
            changed = added & statements if statements else added
            s.changed_code_lines += len(changed)
            if changed:
                s.uncovered_changes[path] = sorted(changed)
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

    # Runtime evidence (logs, traces, latency, metrics, memory) is compared per test,
    # for tests that exist before and after (including ones moved by a file rename),
    # because those ran the same inputs. Each test's errors are compared with that
    # test's own errors before the change. Records outside any test (e.g. at import)
    # are compared with the base run's records outside any test. Tests that newly
    # fail are left out: the failure is already scored.
    base_by_junit = {junit_id(n): n for n in base.node_ids}
    head_by_junit = {junit_id(n): n for n in head.node_ids}
    base_of: Dict[str, str] = {n: n for n in head.node_ids if n in set(base.node_ids)}
    for old, new in s.tests_moved.items():
        if old in base_by_junit and new in head_by_junit:
            base_of[head_by_junit[new]] = base_by_junit[old]
    failing_now = {head_by_junit[j] for j in (*s.tests_newly_failing, *s.tests_flaky) if j in head_by_junit}

    def comparable(test: Optional[str]) -> bool:
        return test is None or (test in base_of and test not in failing_now)

    def expects(test: str, side_source: Dict[str, str], node: str) -> bool:
        return expects_exception(side_source.get(node_file(node)), node)

    # A test expects an exception if its own code says so (pytest.raises /
    # assertRaises): newly in an existing test, or anywhere in a new one.
    expecting = set()
    for t in head.node_ids:
        if t in failing_now or not expects(t, diff.head_source, t):
            continue
        if t not in base_of or not expects(t, diff.base_source, base_of[t]):
            expecting.add(t)

    def in_shared(test: Optional[str]) -> bool:
        return comparable(test) and test not in expecting

    def in_new(test: Optional[str]) -> bool:
        return test is not None and test not in base_of and test not in failing_now and test not in expecting

    base_log_sigs = _signatures_by_test(base.logs, _log_signature, _is_error_log)
    base_exc_sigs = _signatures_by_test(base.spans, None, None)
    every_base_sig = set().union(*base_log_sigs.values(), *base_exc_sigs.values()) if (base_log_sigs or base_exc_sigs) else set()

    def new_per_test(records, signature, keep_record, before, keep_test):
        found: Dict[str, int] = {}
        for test, sig in _record_signatures(records, signature, keep_record):
            if not keep_test(test):
                continue
            seen_before = before.get(base_of.get(test, test) if test else None, set())
            if sig not in seen_before:
                found[sig] = found.get(sig, 0) + 1
        return found

    def new_anywhere(records, signature, keep_record, keep_test):
        found: Dict[str, int] = {}
        for test, sig in _record_signatures(records, signature, keep_record):
            if keep_test(test) and sig not in every_base_sig:
                found[sig] = found.get(sig, 0) + 1
        return found

    s.new_error_logs = new_per_test(head.logs, _log_signature, _is_error_log, base_log_sigs, in_shared)
    s.new_span_exceptions = new_per_test(head.spans, None, None, base_exc_sigs, in_shared)
    s.new_test_error_logs = new_anywhere(head.logs, _log_signature, _is_error_log, in_new)
    s.new_test_span_exceptions = new_anywhere(head.spans, None, None, in_new)
    for found in (new_anywhere(head.logs, _log_signature, _is_error_log, expecting.__contains__),
                  new_anywhere(head.spans, None, None, expecting.__contains__)):
        for sig, n in found.items():
            s.new_test_expected_errors[sig] = s.new_test_expected_errors.get(sig, 0) + n

    for found in (s.new_error_logs, s.new_span_exceptions, s.new_test_error_logs, s.new_test_span_exceptions):
        for sig in [k for k in found if cfg.expected(k)]:
            s.expected_errors[sig] = s.expected_errors.get(sig, 0) + found.pop(sig)
    # An error already scored at full weight for an existing test isn't counted
    # again at half weight because a new test hits it too.
    for scored, half in ((s.new_error_logs, s.new_test_error_logs), (s.new_span_exceptions, s.new_test_span_exceptions)):
        for sig in [k for k in half if k in scored]:
            scored[sig] += half.pop(sig)
    # An error that's scored somewhere isn't also listed as expected and unscored.
    scored_anywhere = {**s.new_error_logs, **s.new_span_exceptions, **s.new_test_error_logs,
                       **s.new_test_span_exceptions}
    s.new_test_expected_errors = {k: v for k, v in s.new_test_expected_errors.items() if k not in scored_anywhere}

    # Timings from a cached base run may come from another machine (in CI, an
    # earlier job's runner), so they aren't comparable with this run's.
    base_tests = set(base_of.values())
    base_lat = _median_durations(base, lambda t: t is None or t in base_tests)
    head_lat = _median_durations(head, comparable)
    if base.from_cache:
        s.latency_skipped = "the base results came from the cache, possibly measured on another machine"
        head_lat = {}
    for name, after in head_lat.items():
        before = base_lat.get(name)
        if before is None or before <= 0:
            continue
        if after >= before * th.latency_ratio and after - before >= th.latency_min_delta_ms:
            s.latency_regressions[name] = {"before_ms": round(before, 2), "after_ms": round(after, 2)}

    # Metrics: compare each series within the same test, which ran the same inputs.
    s.metrics_enabled = head.otel_metrics_enabled and bool(base.metrics or head.metrics)
    base_m, head_m = _metric_values(base), _metric_values(head)
    error_like = {_series(p) for p in head.metrics if _error_like(p)}
    for (test, series), (kind, after) in head_m.items():
        if not comparable(test):
            continue
        before = base_m.get((base_of.get(test, test) if test else None, series), (kind, None))[1]
        if before is None:
            if kind == "counter" and after > 0 and series in error_like:
                s.new_error_metrics[series] = s.new_error_metrics.get(series, 0) + after
            continue
        growth = after - before
        # Ratios only mean something for positive values (an up-down counter can go negative).
        if before > 0 and after >= before * th.metric_ratio and growth >= th.metric_min_delta:
            worst = s.metric_regressions.get(series)
            if worst is None or growth > worst["after"] - worst["before"]:
                s.metric_regressions[series] = {"test": test or "(outside any test)", "kind": kind,
                                                "before": before, "after": after}

    s.memory_enabled = bool(base.memory and head.memory)
    for test, after in head.memory.items():
        if test is None or not comparable(test):
            continue
        before = base.memory.get(base_of[test])
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
    s.code_diff_lines = sum(n for path, n in diff.file_lines.items()
                            if not _is_test_file(path, tests_path) and not _is_docs_or_lockfile(path))
    s.files_changed = diff.files_changed
    return s
