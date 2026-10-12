"""Find new `except` blocks that silence errors without leaving any trace.

A swallowed error that is logged shows up in the runtime evidence. One that is
caught and dropped (`except Exception: pass`) produces no evidence at all, so the
only place to see it is the code.
"""

from __future__ import annotations

import ast
from collections import Counter
from typing import Iterable, List, Set, Tuple

BROAD = {"Exception", "BaseException"}
# Calls that leave a trace of the error: logging, tracing, error reporting.
TRACE_CALLS = {"error", "exception", "critical", "warning", "warn", "fatal",
               "record_exception", "print_exc", "print_exception", "capture_exception"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare except:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in names:
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name in BROAD:
            return True
    return False


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
    for node in _walk_outside_nested_handlers(handler.body):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in TRACE_CALLS:
                return True
            # span.set_status(Status(StatusCode.ERROR)) records the error; resp.set_status(200) doesn't.
            if name == "set_status" and "ERROR" in ast.dump(node):
                return True
    return False


def silent_handlers(source: str, changed_lines: Iterable[int]) -> List[Tuple[int, int]]:
    """(first, last) lines of broad except clauses on changed lines that drop the error silently."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    changed: Set[int] = set(changed_lines)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno in changed:
            if _is_broad(node) and not _leaves_a_trace(node):
                found.append((node.lineno, node.end_lineno or node.lineno))
    return sorted(found)


def silent_excepts(source: str, changed_lines: Iterable[int]) -> List[int]:
    """Line numbers of broad except clauses on changed lines that drop the error silently."""
    return [first for first, _ in silent_handlers(source, changed_lines)]


def new_silent_excepts(path: str, source: str, changed_lines: Iterable[int],
                       removed: Counter) -> List[str]:
    """Silent handlers on changed lines, minus ones this change only moved or re-indented.

    `removed` counts the stripped lines this change deleted anywhere. A handler
    whose lines were all deleted somewhere else is the same code, moved, and is
    consumed from the counter so each deletion explains only one handler.
    """
    lines = source.split("\n")  # the line numbering ast uses (str.splitlines also splits on \f etc.)
    found = []
    for first, last in silent_handlers(source, changed_lines):
        text = [line.strip() for line in lines[first - 1:last] if line.strip()]
        need = Counter(text)
        if all(removed[t] >= n for t, n in need.items()):
            removed.subtract(need)
            continue
        found.append(f"{path}:{first}: {lines[first - 1].strip()}")
    return found
