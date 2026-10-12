"""Mutation testing limited to the lines a change touched.

Coverage only says a line ran. To see whether the tests *check* it, make one
small edit to the line (flip a comparison, swap + and -, True and False, change
a number), run only the tests that ran that line, and see whether any of them
fails. If none does, the mutant "survived": the tests run the code but don't
notice when it's wrong.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import shutil
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .runner import SECRET_ENV, optional_flags, run_pytest

SWAPS = {
    "<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==",
    "+": "-", "-": "+", "*": "/", "/": "*", "//": "*", "%": "*",
    "and": "or", "or": "and", "True": "False", "False": "True", "is": "is not", "in": "not in",
}


# Statements that only report telemetry: logging, span attributes, metric
# recordings. Tests rarely check them and the agent measures them directly, so
# mutating them would only add noise.
LOG_CALLS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
SPAN_CALLS = {"set_attribute", "set_attributes", "add_event", "record_exception", "set_status"}
INSTRUMENT_CALLS = {"add", "record", "set"}
INSTRUMENT_FACTORIES = {"create_counter", "create_up_down_counter", "create_histogram", "create_gauge"}


def _instruments(tree: ast.AST) -> set:
    """Names bound to metric instruments: `x = meter.create_counter(...)` or `self.x = ...`."""
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in INSTRUMENT_FACTORIES):
            names.update(ast.unparse(t) for t in node.targets)
    return names


def telemetry_lines(source: str) -> set:
    """Line numbers of statements that are nothing but a telemetry call.

    Logging calls on a logger-like receiver, span methods, and add/record/set on
    a metric instrument this module created. Ordinary calls such as
    `seen.add(x)` or `cache.set(k, v)` are logic and stay mutable.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    instruments = _instruments(tree)
    lines = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)):
            continue
        method, receiver = node.value.func.attr, ast.unparse(node.value.func.value)
        last = receiver.rsplit(".", 1)[-1].lower().lstrip("_")
        # log, logger, app_log, audit_log, LOGGER, logging, logging.getLogger(__name__)
        logger_like = (last in ("log", "logging") or last.endswith(("_log", "logger"))
                       or last.startswith("getlogger("))
        span_like = "span" in last
        if ((method in LOG_CALLS and logger_like) or (method in SPAN_CALLS and (span_like or method != "set_status"))
                or (method in INSTRUMENT_CALLS and receiver in instruments)):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


@dataclass
class Mutant:
    path: str
    line: int
    col: int
    original: str
    replacement: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.original} → {self.replacement}"


@dataclass
class MutationResult:
    tried: int = 0
    killed: int = 0
    survived: List[str] = field(default_factory=list)  # descriptions of mutants no test noticed
    skipped: int = 0  # couldn't run (no covering test, error, or broken mutant)
    note: str = ""  # why mutation testing didn't run or couldn't conclude, for the report


def _inert_spans(source: str) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Source ranges where an edit can't change behavior: type annotations."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans = []

    def add(node):
        if node is not None and hasattr(node, "lineno"):
            spans.append(((node.lineno, node.col_offset), (node.end_lineno, node.end_col_offset)))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.returns)
            args = node.args
            for a in (*getattr(args, "posonlyargs", []), *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if a is not None:
                    add(a.annotation)
        elif isinstance(node, ast.AnnAssign):
            add(node.annotation)
    return spans


def _inside(spans, row: int, col: int) -> bool:
    return any(start <= (row, col) < end for start, end in spans)


def mutants_for_line(source: str, path: str, line: int) -> List[Mutant]:
    """Every single-token mutation of one line that still compiles and can change behavior."""
    found = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    inert = _inert_spans(source)
    for i, tok in enumerate(tokens):
        if tok.start[0] != line or tok.end[0] != line or _inside(inert, *tok.start):
            continue
        following = tokens[i + 1].string if i + 1 < len(tokens) else ""
        if tok.string in ("*", "/") and following in (",", ")"):
            continue  # a bare `*` or `/` in a parameter list is a marker, not arithmetic
        replacement = None
        if tok.type in (tokenize.OP, tokenize.NAME) and tok.string in SWAPS:
            replacement = SWAPS[tok.string]
        elif tok.type == tokenize.NUMBER and tok.string.isdigit():
            replacement = str(int(tok.string) + 1)
        elif tok.type == tokenize.NUMBER and re.fullmatch(r"\d*\.\d+|\d+\.\d*", tok.string):
            replacement = repr(float(tok.string) + 1)
        if replacement is None:
            continue
        mutant = Mutant(path, line, tok.start[1], tok.string, replacement)
        try:
            compile(apply(source, mutant), path, "exec")
        except SyntaxError:
            continue
        found.append(mutant)
    return found


def apply(source: str, mutant: Mutant) -> str:
    # Same line numbering as tokenize (str.splitlines would also split on form feeds).
    lines = io.StringIO(source).readlines()
    text = lines[mutant.line - 1]
    lines[mutant.line - 1] = text[:mutant.col] + mutant.replacement + text[mutant.col + len(mutant.original):]
    return "".join(lines)


def choose(candidates: Dict[Tuple[str, int], List[Mutant]], limit: int) -> List[Mutant]:
    """Spread the budget across lines: one mutant per line first, then a second, and so on."""
    chosen, round_ = [], 0
    while len(chosen) < limit:
        added = False
        for key in sorted(candidates):
            if round_ < len(candidates[key]) and len(chosen) < limit:
                chosen.append(candidates[key][round_])
                added = True
        if not added:
            break
        round_ += 1
    return chosen


TIMEOUT = "timeout"


def _run(workdir: Path, node_ids: List[str], env: Dict[str, str], timeout: float):
    """True if a test failed, False if all passed, TIMEOUT, or None if it couldn't run."""
    cmd = [sys.executable, "-m", "pytest", *node_ids, "-x", "-q", "-o", f"cache_dir={env['CONFIDENCE_SCRATCH']}"]
    try:
        proc = run_pytest(cmd, optional_flags(), cwd=workdir, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMEOUT
    if proc.returncode == 0:
        return False
    if proc.returncode == 1:
        return True
    return None


def _test_ids(contexts: List[str], failing: Set[str]) -> List[str]:
    """Usable covering tests: pytest node ids that pass without the mutant.

    A project's own coverage settings can prefix contexts ("ci|tests/...") or
    replace them (dynamic_context = "test_function"); prefixes are stripped and
    anything that isn't a node id is dropped. Tests that already fail would
    "catch" every mutant, so they don't count.
    """
    ids = {c.rsplit("|", 1)[-1] for c in contexts if c}
    return sorted(t for t in ids if "::" in t and t not in failing)


def run_mutations(workdir: Path, lines: Dict[str, Iterable[int]], contexts: Dict[str, Dict[str, List[str]]],
                  import_paths: List[str], limit: int = 20, timeout: float = 60.0,
                  failing: Iterable[str] = (), durations: Optional[Dict[str, float]] = None,
                  total_budget: float = 300.0) -> MutationResult:
    """Mutate covered changed lines in the checkout and run the tests that cover each one.

    lines: path -> changed line numbers to consider (already limited to covered source lines).
    contexts: coverage's per-file {line: [test node ids]}.
    failing: node ids that fail (or are flaky) without any mutant; they're never used.
    durations: node id -> seconds in the head run; each mutant gets at least 5x
        its covering tests' normal time, so slow tests aren't mistaken for hangs.
    total_budget: seconds for all mutants together; when it runs out, the rest
        are skipped and the report says how many were tested.
    """
    failing = set(failing)
    durations = durations or {}
    candidates: Dict[Tuple[str, int], List[Mutant]] = {}
    sources: Dict[str, str] = {}
    originals: Dict[str, bytes] = {}
    encodings: Dict[str, str] = {}
    unusable = False
    for path, nums in lines.items():
        file = workdir / path
        if not file.is_file():
            continue
        originals[path] = file.read_bytes()
        encodings[path] = tokenize.detect_encoding(io.BytesIO(originals[path]).readline)[0]
        try:
            sources[path] = originals[path].decode(encodings[path])
        except (UnicodeDecodeError, LookupError):
            continue
        skip = telemetry_lines(sources[path])
        for line in nums:
            if line in skip:
                continue
            raw = contexts.get(path, {}).get(str(line), [])
            tests = _test_ids(raw, failing)
            if not tests and any(raw):
                unusable = True
            if tests:
                found = mutants_for_line(sources[path], path, line)
                if found:
                    candidates[(path, line)] = found
    result = MutationResult()
    chosen = choose(candidates, limit)
    if not chosen:
        if unusable:
            result.note = ("skipped: the changed lines' covering tests either fail without any change or "
                           "aren't recorded as pytest test ids (does your coverage config set dynamic_context?)")
        return result

    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (*import_paths, os.environ.get("PYTHONPATH", "")) if p)
    env.pop("PYTEST_ADDOPTS", None)
    scratch = tempfile.mkdtemp(prefix="confidence-mutate-")
    env["CONFIDENCE_SCRATCH"] = scratch
    clean: Dict[Tuple[str, ...], bool] = {}  # covering test set -> passes without any mutant
    started = time.monotonic()
    for i, mutant in enumerate(chosen):
        left = total_budget - (time.monotonic() - started)
        if i and left <= 0:  # always try at least one
            result.note = (f"stopped after {i} of {len(chosen)} mutants: the {total_budget:g}s mutation "
                           "budget ran out (mutation_budget_s)")
            break
        file = workdir / mutant.path
        tests = _test_ids(contexts[mutant.path][str(mutant.line)], failing)
        normal = max(timeout, 5 * sum(durations.get(t, 0.0) for t in tests) + 10)
        budget = min(normal, left) if i else normal  # the overall budget is a hard limit after the first
        key = tuple(tests)
        if key not in clean:
            # These tests passed in the full run, but may depend on another test
            # having run first. Run them alone, unmutated, once: if they fail
            # here, they'd "catch" every mutant for the wrong reason.
            with tempfile.TemporaryDirectory(prefix="confidence-pyc-") as pyc:
                env["PYTHONPYCACHEPREFIX"] = pyc
                clean[key] = _run(workdir, tests, env, normal) is False
        if not clean[key]:
            result.tried += 1
            result.skipped += 1
            continue
        with tempfile.TemporaryDirectory(prefix="confidence-pyc-") as pyc:
            # A fresh bytecode cache: same-size edits made within one second could
            # otherwise reuse the cached, unmutated bytecode.
            env["PYTHONPYCACHEPREFIX"] = pyc
            try:
                file.write_bytes(apply(sources[mutant.path], mutant).encode(encodings[mutant.path]))
                killed = _run(workdir, tests, env, budget)
            finally:
                file.write_bytes(originals[mutant.path])  # byte for byte, line endings included
        if killed == TIMEOUT:
            # A hang the tests noticed, unless it was the overall budget that cut the run short.
            killed = None if budget < normal else True
        result.tried += 1
        if killed is None:
            result.skipped += 1
        elif killed:
            result.killed += 1
        else:
            result.survived.append(mutant.describe())
    shutil.rmtree(scratch, ignore_errors=True)
    if result.tried and result.skipped == result.tried:
        none = f"none of the {result.tried} mutants could be tested (the covering tests didn't run cleanly)"
        result.note = f"{result.note}; {none}" if result.note else none
    return result
