"""Changes the test run can't judge: dependencies, risky code, config files.

Tests and telemetry say nothing about a new third-party package, a call to
eval(), or an edited YAML file. These checks read the diff instead.
"""

from __future__ import annotations

import ast
import re
import sys
from fnmatch import fnmatch
from typing import Dict, Iterable, List, Optional, Set

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib

# ---------------------------------------------------------------- dependencies

REQUIREMENTS = ("requirements*.txt", "*/requirements*.txt", "requirements/*.txt", "*.in")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(text: str) -> Dict[str, str]:
    deps = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        m = _REQ_NAME.match(line)
        if m:
            deps[_norm(m.group(1))] = m.group(3).strip()
    return deps


def _pyproject(text: str) -> Dict[str, str]:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    project = doc.get("project", {})
    lines = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        lines += extra
    deps = _requirements("\n".join(lines))
    for name, spec in doc.get("tool", {}).get("poetry", {}).get("dependencies", {}).items():
        if _norm(name) != "python":
            deps[_norm(name)] = spec if isinstance(spec, str) else str(spec)
    return deps


def is_dependency_file(path: str) -> bool:
    return path.endswith("pyproject.toml") or any(fnmatch(path, p) for p in REQUIREMENTS)


def dependency_changes(path: str, base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """Human-readable added or changed dependencies in one file."""
    parse = _pyproject if path.endswith("pyproject.toml") else _requirements
    before, after = parse(base_text or ""), parse(head_text or "")
    changes = []
    for name in sorted(after):
        if name not in before:
            changes.append(f"{path}: added {name} {after[name]}".rstrip())
        elif after[name] != before[name]:
            changes.append(f"{path}: {name} {before[name] or '(any)'} → {after[name] or '(any)'}")
    return changes


# ---------------------------------------------------------------- risky code

SECRET_NAME = re.compile(r"(password|passwd|secret|api_?key|token|private_?key)", re.IGNORECASE)


def _call_name(node: ast.Call) -> str:
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _keyword(node: ast.Call, name: str):
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_const(node, value) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _risk(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in ("eval", "exec"):
            return f"{name}() runs arbitrary code"
        if name in ("pickle.load", "pickle.loads", "cPickle.loads", "dill.loads", "marshal.loads"):
            return f"{name}() can run code from untrusted data"
        if name == "yaml.load" and not any(kw.arg == "Loader" and "Safe" in ast.dump(kw.value)
                                           for kw in node.keywords):
            return "yaml.load() without SafeLoader can run code"
        if name in ("os.system", "os.popen"):
            return f"{name}() runs a shell command"
        if name.startswith("subprocess.") and _is_const(_keyword(node, "shell"), True):
            return f"{name}(shell=True) runs a shell command"
        if _is_const(_keyword(node, "verify"), False):
            return f"{name}(verify=False) turns off TLS certificate checks"
        if name in ("hashlib.md5", "hashlib.sha1") or (name == "hashlib.new" and node.args
                                                      and _is_const(node.args[0], "md5")):
            return f"{name}() is a weak hash"
        if name == "tempfile.mktemp":
            return "tempfile.mktemp() is open to file races; use mkstemp"
        if name == "jsonpickle.decode":
            return "jsonpickle.decode() can run code from untrusted data"
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 8:
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if name and SECRET_NAME.search(name):
                    return f"hard-coded secret in {name}"
    return None


def risky_code(path: str, source: str, changed: Iterable[int]) -> List[str]:
    """Security-sensitive patterns on changed lines (a small subset of what bandit checks)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    changed_set: Set[int] = set(changed)
    found = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in changed_set:
            reason = _risk(node)
            if reason:
                found.append(f"{path}:{node.lineno}: {reason}")
    return sorted(set(found), key=lambda f: (f.split(":")[0], int(f.split(":")[1])))


# ---------------------------------------------------------------- unmeasured files

CODE = (".py",)
DOCS = ("*.md", "*.rst", "*.txt", "docs/*", "LICENSE*", ".gitignore")


def unmeasured(paths: Iterable[str]) -> List[str]:
    """Changed files that tests can't exercise: config, data, CI, infrastructure."""
    out = []
    for path in paths:
        if path.endswith(CODE) or is_dependency_file(path) or any(fnmatch(path, d) for d in DOCS):
            continue
        out.append(path)
    return sorted(out)
