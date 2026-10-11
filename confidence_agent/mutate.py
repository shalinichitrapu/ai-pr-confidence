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
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SWAPS = {
    "<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==",
    "+": "-", "-": "+", "*": "/", "/": "*", "//": "*", "%": "*",
    "and": "or", "or": "and", "True": "False", "False": "True", "is": "is not", "in": "not in",
}


# Statements that only report telemetry: logging, span attributes, metric
# recordings. Tests rarely check them and the agent measures them directly, so
# mutating them would only add noise.
TELEMETRY_CALLS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log",
                   "set_attribute", "set_attributes", "add_event", "record_exception", "set_status",
                   "add", "record", "set"}


def telemetry_lines(source: str) -> set:
    """Line numbers of statements that are nothing but a telemetry call."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in TELEMETRY_CALLS):
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
    lines = source.splitlines(keepends=True)
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
    cmd = [sys.executable, "-m", "pytest", *node_ids, "-x", "-q", "-p", "no:cacheprovider"]
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


def run_mutations(workdir: Path, lines: Dict[str, Iterable[int]], contexts: Dict[str, Dict[str, List[str]]],
                  import_paths: List[str], limit: int = 20, timeout: float = 60.0) -> MutationResult:
    """Mutate covered changed lines in the checkout and run the tests that cover each one.

    lines: path -> changed line numbers to consider (already limited to covered source lines).
    contexts: coverage's per-file {line: [test node ids]}.
    """
    candidates: Dict[Tuple[str, int], List[Mutant]] = {}
    sources: Dict[str, str] = {}
    for path, nums in lines.items():
        file = workdir / path
        if not file.is_file():
            continue
        sources[path] = file.read_text()
        skip = telemetry_lines(sources[path])
        for line in nums:
            if line in skip:
                continue
            tests = [c for c in contexts.get(path, {}).get(str(line), []) if c]
            if tests:
                found = mutants_for_line(sources[path], path, line)
                if found:
                    candidates[(path, line)] = found
    result = MutationResult()
    chosen = choose(candidates, limit)
    if not chosen:
        return result

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (*import_paths, env.get("PYTHONPATH", "")) if p)
    env.pop("PYTEST_ADDOPTS", None)
    for mutant in chosen:
        file = workdir / mutant.path
        tests = sorted({c for c in contexts[mutant.path][str(mutant.line)] if c})
        with tempfile.TemporaryDirectory(prefix="confidence-pyc-") as pyc:
            # A fresh bytecode cache: same-size edits made within one second could
            # otherwise reuse the cached, unmutated bytecode.
            env["PYTHONPYCACHEPREFIX"] = pyc
            try:
                file.write_text(apply(sources[mutant.path], mutant))
                killed = _run(workdir, tests, env, timeout)
            finally:
                file.write_text(sources[mutant.path])
        result.tried += 1
        if killed is None:
            result.skipped += 1
        elif killed:
            result.killed += 1
        else:
            result.survived.append(mutant.describe())
    return result
