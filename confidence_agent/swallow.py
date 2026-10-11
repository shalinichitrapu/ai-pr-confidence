"""Find new `except` blocks that silence errors without leaving any trace.

A swallowed error that is logged shows up in the runtime evidence. One that is
caught and dropped (`except Exception: pass`) produces no evidence at all, so the
only place to see it is the code.
"""

from __future__ import annotations

import ast
from typing import Iterable, List, Set

BROAD = {"Exception", "BaseException"}
# Calls that leave a trace of the error: logging, tracing, error reporting.
TRACE_CALLS = {"error", "exception", "critical", "warning", "warn", "fatal",
               "record_exception", "set_status", "print_exc", "print_exception", "capture_exception"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare except:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for node in names:
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name in BROAD:
            return True
    return False


def _leaves_a_trace(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in TRACE_CALLS:
                return True
    return False


def silent_excepts(source: str, changed_lines: Iterable[int]) -> List[int]:
    """Line numbers of broad except clauses on changed lines that drop the error silently."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    changed: Set[int] = set(changed_lines)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno in changed:
            if _is_broad(node) and not _leaves_a_trace(node):
                found.append(node.lineno)
    return sorted(found)
