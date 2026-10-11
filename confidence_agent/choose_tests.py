"""Run only the tests a change can affect (opt-in: --select-tests or select_tests = true).

The base run records which tests executed which lines, and coverage groups
lines by function. A test can only be affected by a change to a function body if
it ran that function, so for those changes the head run can skip the rest. Anything this can't trace safely falls
back to the full suite:

- module-level or class-level changes other than imports (a test may read a
  constant without running any line of the file); a changed import only
  affects that file, so it selects every test that ran the file, and a
  brand-new file can't affect existing tests at all,
- a changed conftest.py,
- changed or deleted Python files outside --source,
- changed config, dependency or data files,
- a base run without per-test coverage, or an empty selection.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from fnmatch import fnmatch
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .config import Config
from .gitutil import Diff
from .runner import RunResult
from .signals import _in_source, _is_test_file

DOCS = ("*.md", "*.rst", "LICENSE*", "docs/*", ".gitignore")


@dataclasses.dataclass
class Selection:
    node_ids: List[str]  # tests chosen from the base run's coverage
    test_files: List[str]  # changed test files, run whole (they may add or remove tests)

    def targets(self) -> List[str]:
        covered_files = set(self.test_files)
        return sorted(self.test_files) + [n for n in self.node_ids if n.split("::", 1)[0] not in covered_files]


_DEFINITION = re.compile(r"^\s*(async\s+def|def|class)\s")


def _function_at(source: str) -> Optional[Dict[int, str]]:
    """Line -> qualified name of the innermost function that holds it ("Box.g").

    Names match coverage's per-function keys. A function's decorator and def
    lines count as part of it. Lines outside any function (module level, class
    level) are absent.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    where: Dict[int, str] = {}

    def visit(nodes, prefix):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                for line in range(start, (node.end_lineno or start) + 1):
                    where[line] = name  # inner functions overwrite below
                visit(node.body, name + ".")
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    visit(getattr(node, field, []) or [], prefix)

    visit(tree.body, "")
    return where


def _import_lines(source: str) -> Set[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {line for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)}


def _tests_for(coverage: dict, lines: Iterable[int]) -> Set[str]:
    contexts = coverage.get("contexts", {})
    return {t for line in lines for t in contexts.get(str(line), []) if t}


def plan(diff: Diff, base: RunResult, tests_path: str, source: str,
         cfg: Optional[Config] = None) -> Tuple[Optional[Selection], str]:
    """(selection, reason). A None selection means run everything, and the reason says why."""
    cfg = cfg or Config()
    if not base.node_ids or not any(f.get("contexts") for f in base.coverage.values()):
        return None, "the base run has no per-test coverage"
    chosen: Set[str] = set()
    test_files: List[str] = []
    for path in diff.files_changed:
        if cfg.ignored(path) or any(fnmatch(path, d) for d in DOCS):
            continue
        if not path.endswith(".py"):
            return None, f"{path} isn't Python code, so any test may depend on it"
        if path not in diff.head_source:
            return None, f"{path} was deleted"
        if _is_test_file(path, tests_path):
            if path.rsplit("/", 1)[-1] == "conftest.py":
                return None, f"{path} can change every test"
            test_files.append(path)
            continue
        if not _in_source(path, source):
            return None, f"{path} is outside --source, so its effect on tests isn't traced"
        functions = _function_at(diff.head_source[path])
        if functions is None:
            return None, f"{path} doesn't parse"
        text = diff.head_source[path].splitlines()
        changed = {n for n in diff.added_lines.get(path, set())
                   if n <= len(text) and text[n - 1].strip() and not text[n - 1].strip().startswith("#")}
        if path not in base.coverage and not diff.removed_text.get(path):
            continue  # a new file: no existing test can depend on it yet
        imports = _import_lines(diff.head_source[path])
        outside = sorted(changed - set(functions) - imports)
        if outside:
            return None, f"{path}:{outside[0]} changes module- or class-level code, which any test may read"
        cov = base.coverage.get(path, {})
        by_function = cov.get("functions")
        removed_definition = any(_DEFINITION.match(line) for line in diff.removed_text.get(path, []))
        if by_function is None or removed_definition or changed & imports:
            # A function was removed or renamed, an import changed, or coverage has no
            # per-function data: every test that ran this file may be affected.
            chosen.update(_tests_for(cov, cov.get("contexts", {}).keys()))
            continue
        for name in {functions[line] for line in changed}:
            chosen.update(_tests_for(cov, by_function.get(name, {}).get("executed_lines", [])))
    if not chosen and not test_files:
        return None, "nothing to select"
    return Selection(sorted(chosen), sorted(test_files)), ""


def junit_id(node_id: str) -> str:
    """pytest's "tests/test_a.py::TestX::test_f" -> JUnit's "tests.test_a.TestX::test_f"."""
    path, *rest = node_id.split("::")
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path
    return "::".join([".".join([module, *rest[:-1]]), rest[-1]]) if rest else node_id


def restrict(base: RunResult, selection: Selection) -> RunResult:
    """The base run limited to the tests the head run will run, so unselected tests don't look removed."""
    files = set(selection.test_files)
    keep = {n for n in base.node_ids if n in selection.node_ids or n.split("::", 1)[0] in files}
    keep_junit = {junit_id(n) for n in keep}
    file_modules = {f[:-3].replace("/", ".") for f in files}

    def junit_kept(test_id: str) -> bool:
        classname = test_id.rpartition("::")[0]
        return test_id in keep_junit or any(classname == m or classname.startswith(m + ".") for m in file_modules)

    def by_test(records: Iterable[dict]) -> list:
        return [r for r in records if r.get("test") is None or r["test"] in keep]

    return dataclasses.replace(
        base,
        tests={t: o for t, o in base.tests.items() if junit_kept(t)},
        test_durations={t: d for t, d in base.test_durations.items() if junit_kept(t)},
        node_ids=[n for n in base.node_ids if n in keep],
        spans=by_test(base.spans),
        logs=by_test(base.logs),
        metrics=by_test(base.metrics),
        memory={t: m for t, m in base.memory.items() if t in keep},
    )
