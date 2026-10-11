"""Does a test expect an exception? Read from the test's own source.

Errors that show up only in a new test are often the point of the test
(`with pytest.raises(...)`). When the test says so, they aren't scored.
"""

from __future__ import annotations

import ast
from typing import Optional

RAISES = {"raises", "assertRaises", "assertRaisesRegex", "assertRaisesRegexp"}


def _split(node_id: str):
    """tests/test_a.py::TestX::test_f[1] -> ("tests/test_a.py", ["TestX"], "test_f")."""
    parts = node_id.split("::")
    return parts[0], parts[1:-1], parts[-1].split("[", 1)[0]


def _find(body, classes, name) -> Optional[ast.AST]:
    for node in body:
        if classes and isinstance(node, ast.ClassDef) and node.name == classes[0]:
            return _find(node.body, classes[1:], name)
        if not classes and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def expects_exception(source: Optional[str], node_id: str) -> bool:
    """True if the test's function uses pytest.raises or unittest's assertRaises."""
    if not source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    _, classes, name = _split(node_id)
    func = _find(tree.body, classes, name)
    if func is None:
        return False
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if called in RAISES:
                return True
    return False


def node_file(node_id: str) -> str:
    return _split(node_id)[0]
