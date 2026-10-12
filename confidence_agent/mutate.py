"""Mutation testing limited to the lines a change touched.

Coverage only says a line ran. To see whether the tests *check* it, make one
small edit to the line (flip a comparison, swap + and -, True and False, change
a number), run only the tests that ran that line, and see whether any of them
fails. If none does, the mutant "survived": the tests run the code but don't
notice when it's wrong.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

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
        logger_like = last in ("log", "logger", "logging") or last.endswith("logger")
        if ((method in LOG_CALLS and logger_like) or method in SPAN_CALLS
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
        return f"{self.path}:{self.line}: `{self.original}` → `{self.replacement}`"


@dataclass
class MutationResult:
    tried: int = 0
    killed: int = 0
    survived: List[str] = field(default_factory=list)  # descriptions of mutants no test noticed
    skipped: int = 0  # couldn't run (no covering test, error, or broken mutant)
    note: str = ""  # why mutation testing didn't run or couldn't conclude, for the report


def mutants_for_line(source: str, path: str, line: int) -> List[Mutant]:
    """Every single-token mutation of one line that still compiles."""
    found = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    for tok in tokens:
        if tok.start[0] != line or tok.end[0] != line:
            continue
        replacement = None
        if tok.type in (tokenize.OP, tokenize.NAME) and tok.string in SWAPS:
            replacement = SWAPS[tok.string]
        elif tok.type == tokenize.NUMBER and tok.string.isdigit():
            replacement = str(int(tok.string) + 1)
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


def _run(workdir: Path, node_ids: List[str], env: Dict[str, str], timeout: float) -> Optional[bool]:
    """True if a test failed (mutant killed), False if all passed, None if it couldn't run."""
    cmd = [sys.executable, "-m", "pytest", *node_ids, "-x", "-q", "-p", "no:cacheprovider",
           "-p", "no:randomly", "-p", "no:random_order"]
    if importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", "0"]
    try:
        proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return True  # the mutant made the tests hang, which they noticed
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
    unusable = False
    for path, nums in lines.items():
        file = workdir / path
        if not file.is_file():
            continue
        sources[path] = file.read_text()
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

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (*import_paths, env.get("PYTHONPATH", "")) if p)
    env.pop("PYTEST_ADDOPTS", None)
    started = time.monotonic()
    for i, mutant in enumerate(chosen):
        if i and time.monotonic() - started > total_budget:  # always try at least one
            result.note = (f"stopped after {i} of {len(chosen)} mutants: the {total_budget:g}s mutation "
                           "budget ran out (mutation_budget_s)")
            break
        file = workdir / mutant.path
        tests = _test_ids(contexts[mutant.path][str(mutant.line)], failing)
        budget = max(timeout, 5 * sum(durations.get(t, 0.0) for t in tests) + 10)
        with tempfile.TemporaryDirectory(prefix="confidence-pyc-") as pyc:
            # A fresh bytecode cache: same-size edits made within one second could
            # otherwise reuse the cached, unmutated bytecode.
            env["PYTHONPYCACHEPREFIX"] = pyc
            try:
                file.write_text(apply(sources[mutant.path], mutant))
                killed = _run(workdir, tests, env, budget)
            finally:
                file.write_text(sources[mutant.path])
        result.tried += 1
        if killed is None:
            result.skipped += 1
        elif killed:
            result.killed += 1
        else:
            result.survived.append(mutant.describe())
    if result.tried and result.skipped == result.tried and not result.note:
        result.note = f"none of the {result.tried} mutants could be tested (the covering tests didn't run cleanly)"
    return result
