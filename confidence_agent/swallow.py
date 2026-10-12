"""Find new code that silences errors without leaving any trace.

A swallowed error that is logged shows up in the runtime evidence. One that is
caught and dropped (`except Exception: pass`, `with suppress(Exception):`)
produces no evidence at all, so the only place to see it is the code.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set, Tuple

BROAD = {"Exception", "BaseException"}
# In tests, catching these hides a failing assertion.
TEST_FAILURES = {"BaseException", "AssertionError", "Exception"}
# Calls that leave a trace of the error: logging, tracing, error reporting.
TRACE_CALLS = {"error", "exception", "critical", "warning", "warn", "fatal",
               "record_exception", "print_exc", "print_exception", "capture_exception"}
LOGGER_NAMES = ("log", "logger", "logging")
# Mentioning the error here isn't handling it: it's still dropped.
QUIET_CALLS = {"debug", "info", "print"}


def _quiet_call_args(handler: ast.ExceptHandler) -> set:
    """ids of nodes inside the arguments of debug/info/print calls."""
    inside = set()
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in QUIET_CALLS:
                for arg in (*node.args, *[k.value for k in node.keywords]):
                    inside.update(id(n) for n in ast.walk(arg))
    return inside


@dataclass(frozen=True)
class Silencer:
    first: int
    last: int
    kind: str  # "broad", "narrow", "suppress" or "test"
    identity: str  # the code's structure, for matching moved or re-indented code


def _names(node) -> List[str]:
    nodes = node.elts if isinstance(node, ast.Tuple) else [node]
    return [n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "") for n in nodes]


def _walk_outside_nested_handlers(nodes):
    """Like ast.walk, but skips the bodies of nested except clauses.

    A `raise` or log call inside a nested handler is about that inner error, not
    the one this handler caught.
    """
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.ExceptHandler):
                stack.append(child)


def _leaves_a_trace(handler: ast.ExceptHandler) -> bool:
    quiet = _quiet_call_args(handler)
    for node in _walk_outside_nested_handlers(handler.body):
        if isinstance(node, ast.Raise):
            return True
        # The caught error is passed on: returned, stored, handed to a callback.
        # (Only mentioned in a debug/info log or a print doesn't count.)
        if handler.name and isinstance(node, ast.Name) and node.id == handler.name and id(node) not in quiet:
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in TRACE_CALLS:
                return True
            if name == "log" and isinstance(func, ast.Attribute):
                receiver = ast.unparse(func.value).rsplit(".", 1)[-1].lower().lstrip("_")
                if receiver in LOGGER_NAMES or receiver.endswith(("_log", "logger")):
                    return True
            # span.set_status(Status(StatusCode.ERROR)) records the error; resp.set_status(200) doesn't.
            if name == "set_status" and "ERROR" in ast.dump(node):
                return True
    return False


def _touches(node, changed: Set[int]) -> bool:
    return any(n in changed for n in range(node.lineno, (node.end_lineno or node.lineno) + 1))


def silencers(source: str, changed_lines: Iterable[int], in_test: bool = False) -> List[Silencer]:
    """Code on changed lines that drops errors without a trace.

    - "broad": `except`/`except Exception`/`except BaseException` that neither
      raises, logs, records nor passes the error on;
    - "narrow": the same for a narrower exception (only a problem in context,
      decided by the caller);
    - "suppress": `with contextlib.suppress(Exception)`;
    - "test": in test code, catching assertion failures silently.
    A handler counts as changed if any of its lines changed, so turning
    `raise` into `pass` is caught.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    changed: Set[int] = set(changed_lines)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _touches(node, changed) and not _leaves_a_trace(node):
            names = set(_names(node.type)) if node.type is not None else {"BaseException"}
            if in_test:
                kind = "test" if names & TEST_FAILURES else ""
            else:
                kind = "broad" if names & BROAD else "narrow"
            if kind:
                found.append(Silencer(node.lineno, node.end_lineno or node.lineno, kind, ast.dump(node)))
        elif isinstance(node, (ast.With, ast.AsyncWith)) and not in_test:
            for item in node.items:
                call = item.context_expr
                if (isinstance(call, ast.Call) and _names(call.func) == ["suppress"]
                        and any(set(_names(a)) & BROAD for a in call.args) and node.lineno in changed):
                    found.append(Silencer(node.lineno, node.lineno, "suppress", ast.dump(item)))
    return sorted(found, key=lambda f: f.first)


def silent_handlers(source: str, changed_lines: Iterable[int]) -> List[Tuple[int, int]]:
    """(first, last) lines of broad silent handlers on changed lines."""
    return [(f.first, f.last) for f in silencers(source, changed_lines) if f.kind in ("broad", "suppress")]


def silent_excepts(source: str, changed_lines: Iterable[int]) -> List[int]:
    """Line numbers of broad silent handlers on changed lines."""
    return [first for first, _ in silent_handlers(source, changed_lines)]


def removed_silent_handlers(base_sources: Dict[str, str], removed_lines: Dict[str, Iterable[int]],
                            skip=lambda path: False) -> Counter:
    """Silent code this change deleted from the base, by its structure."""
    found: Counter = Counter()
    for path, source in base_sources.items():
        if not skip(path):
            for f in silencers(source, removed_lines.get(path, ())):
                found[f.identity] += 1
    return found


def new_silent_excepts(path: str, source: str, changed_lines: Iterable[int], removed: Counter,
                       narrow_too: bool = False, in_test: bool = False) -> List[str]:
    """Silent code on changed lines, minus code this change only moved or re-indented.

    `removed` counts silent code deleted from the base (see removed_silent_handlers);
    new code with the same structure is that code, moved, and each deletion
    explains only one new instance. A handler that used to log or raise and no
    longer does is new, even if its other lines are unchanged. Narrow handlers
    count only when `narrow_too` (the change also dropped error handling nearby).
    """
    lines = source.split("\n")  # the line numbering ast uses (str.splitlines also splits on \f etc.)
    found = []
    for f in silencers(source, changed_lines, in_test=in_test):
        if f.kind == "narrow" and not narrow_too:
            continue
        if removed[f.identity] > 0:
            removed[f.identity] -= 1
            continue
        found.append(f"{path}:{f.first}: {lines[f.first - 1].strip()}")
    return found
