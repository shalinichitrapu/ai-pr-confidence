"""Changes the test run can't judge: dependencies, risky code, config files.

Tests and telemetry say nothing about a new third-party package, a call to
eval(), or an edited CI file. These checks read the diff instead.
"""

from __future__ import annotations

import ast
import re
import sys
from fnmatch import fnmatch
from typing import Dict, Iterable, List, Optional, Set, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib

# ---------------------------------------------------------------- dependencies

REQUIREMENTS = (
    "requirements*.txt", "*/requirements*.txt", "requirements/*.txt",
    "*-requirements*.txt", "*/*-requirements*.txt", "*_requirements*.txt",
    "requirements*.in", "*/requirements*.in", "requirements/*.in",
    "constraints*.txt", "*/constraints*.txt",
)
_REQ_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")
# Requirement lines that aren't "name spec": kept whole, because they're the
# riskiest kind (a git repo, a URL, a local path, another file, a package index).
_RAW = ("-e", "--editable", "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
        "--extra-index-url", "-f", "--find-links", "git+", "hg+", "svn+", "http://", "https://",
        "file:", "./", "../", "/")

Deps = Dict[str, str]  # key (name, plus ";marker" if any) -> normalized spec


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(line: str) -> Optional[Tuple[str, str]]:
    """(key, spec) for one requirement line, or None to skip it."""
    line = line.split(" #", 1)[0].split("\t#", 1)[0].strip().rstrip("\\").strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith(_RAW):
        return " ".join(line.split()), ""
    if line.startswith("-"):
        return None  # --hash=…, --no-binary and similar don't change what's installed
    m = _REQ_NAME.match(line)
    if not m:
        return None
    spec, _, marker = m.group(3).partition(";")
    key = _norm(m.group(1)) + (f";{''.join(marker.split())}" if marker.strip() else "")
    return key, "".join(spec.split())


def _requirements(text: str) -> Deps:
    deps = {}
    for line in text.splitlines():
        parsed = _requirement(line)
        if parsed:
            deps[parsed[0]] = parsed[1]
    return deps


def _poetry(table) -> Deps:
    deps = {}
    for name, spec in (table or {}).items():
        if _norm(name) != "python":
            deps[_norm(name)] = spec if isinstance(spec, str) else repr(sorted(spec.items()) if isinstance(spec, dict) else spec)
    return deps


def _pyproject(text: str) -> Deps:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    project = doc.get("project", {})
    lines = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        lines += extra
    for group in doc.get("dependency-groups", {}).values():  # PEP 735
        lines += [item for item in group if isinstance(item, str)]
    lines += doc.get("build-system", {}).get("requires", [])
    deps = _requirements("\n".join(lines))
    poetry = doc.get("tool", {}).get("poetry", {})
    deps.update(_poetry(poetry.get("dependencies")))
    deps.update(_poetry(poetry.get("dev-dependencies")))
    for group in poetry.get("group", {}).values():
        deps.update(_poetry(group.get("dependencies")))
    return deps


def is_dependency_file(path: str) -> bool:
    return path.endswith("pyproject.toml") or any(fnmatch(path, p) for p in REQUIREMENTS)


def _parse(path: str, text: Optional[str]) -> Deps:
    return (_pyproject if path.endswith("pyproject.toml") else _requirements)(text or "")


def all_dependency_changes(files: Iterable[Tuple[str, Optional[str], Optional[str]]]) -> List[str]:
    """Added or changed dependencies across several files: (path, base text, head text).

    Compared across all the files together, so moving a dependency from one file
    to another isn't reported as new.
    """
    before: Dict[str, str] = {}
    after: Dict[str, Tuple[str, str]] = {}
    for path, base_text, head_text in files:
        before.update(_parse(path, base_text))
        after.update({k: (v, path) for k, v in _parse(path, head_text).items()})
    changes = []
    for key in sorted(after):
        spec, path = after[key]
        if key not in before:
            changes.append(f"{path}: added {key} {spec}".rstrip())
        elif spec != before[key]:
            changes.append(f"{path}: {key} {before[key] or '(any)'} → {spec or '(any)'}")
    return changes


def dependency_changes(path: str, base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """Human-readable added or changed dependencies in one file."""
    return all_dependency_changes([(path, base_text, head_text)])


# ---------------------------------------------------------------- risky code

SECRET_NAME = re.compile(r"(password|passwd|secret|api_?key|token|private_?key)", re.IGNORECASE)
NOT_A_SECRET = re.compile(r"^(https?://|/|[A-Z0-9_]+$)")  # URLs, paths, env var names


def _aliases(tree: ast.AST) -> Dict[str, str]:
    """Local name -> full dotted name, from imports: `import pickle as pk`, `from subprocess import run`."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _call_name(node: ast.Call, aliases: Dict[str, str]) -> str:
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(aliases.get(target.id, target.id))
    return ".".join(reversed(parts))


def _keyword(node: ast.Call, name: str):
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_const(node, value) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _risk(node: ast.AST, aliases: Dict[str, str]) -> Optional[Tuple[str, Set[int]]]:
    """(reason, the lines that make it risky) or None."""
    if isinstance(node, ast.Call):
        name = _call_name(node, aliases)
        at = {node.lineno}
        if name in ("eval", "exec", "builtins.eval", "builtins.exec"):
            return f"{name}() runs arbitrary code", at
        if name in ("pickle.load", "pickle.loads", "cPickle.loads", "dill.loads", "marshal.loads",
                    "jsonpickle.decode", "yaml.unsafe_load"):
            return f"{name}() can run code from untrusted data", at
        if name == "yaml.load":
            loader = _keyword(node, "Loader") or (node.args[1] if len(node.args) > 1 else None)
            if loader is None or "Safe" not in ast.dump(loader):
                return "yaml.load() without SafeLoader can run code", at | ({loader.lineno} if loader else set())
        if name in ("os.system", "os.popen"):
            return f"{name}() runs a shell command", at
        shell = _keyword(node, "shell")
        if name.startswith("subprocess.") and _is_const(shell, True):
            return f"{name}(shell=True) runs a shell command", at | {shell.lineno}
        verify = _keyword(node, "verify")
        if _is_const(verify, False):
            return f"{name}(verify=False) turns off TLS certificate checks", at | {verify.lineno}
        weak = name in ("hashlib.md5", "hashlib.sha1") or (
            name == "hashlib.new" and node.args and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).lower() in ("md5", "sha1"))
        if weak and not _is_const(_keyword(node, "usedforsecurity"), False):
            return f"{name}() is a weak hash", at
        if name == "tempfile.mktemp":
            return "tempfile.mktemp() is open to file races; use mkstemp", at
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if (isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 8
                and " " not in value.value and not NOT_A_SECRET.match(value.value)):
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if name and SECRET_NAME.search(name):
                    return f"hard-coded secret in {name}", {node.lineno}
    return None


def risky_code(path: str, source: str, changed: Iterable[int]) -> List[str]:
    """Security-sensitive patterns this change introduced (a small subset of what bandit checks).

    A finding counts when the call's line or the risky argument's line changed,
    so adding `shell=True` on its own line inside an existing call is caught.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    aliases = _aliases(tree)
    changed_set: Set[int] = set(changed)
    found = set()
    for node in ast.walk(tree):
        if not hasattr(node, "lineno"):
            continue
        risk = _risk(node, aliases)
        if risk and risk[1] & changed_set:
            found.add((min(risk[1] & changed_set), risk[0]))
    return [f"{path}:{line}: {reason}" for line, reason in sorted(found)]


# ---------------------------------------------------------------- unmeasured files

# Infrastructure and configuration that tests don't load. Templates, fixtures and
# other data files are often exercised by tests, so they aren't listed.
INFRA = (".github/*", ".gitlab-ci.yml", ".circleci/*", "Dockerfile*", "*/Dockerfile*", "docker-compose*",
         "*.yml", "*.yaml", "*.ini", "*.cfg", "*.env", ".env*", "Makefile", "*.tf", "*.sh", "Procfile",
         "*.toml")


def unmeasured(paths: Iterable[str]) -> List[str]:
    """Changed CI, deployment and configuration files that tests can't exercise."""
    return sorted(p for p in paths
                  if not is_dependency_file(p) and any(fnmatch(p, pattern) for pattern in INFRA))


def config_only(paths: Iterable[str]) -> bool:
    """True if every changed file is configuration/infrastructure (docs and dependency files aside)."""
    docs = ("*.md", "*.rst", "README*", "CHANGELOG*", "LICENSE*", "docs/*", ".gitignore")
    rest = [p for p in paths if not is_dependency_file(p) and not any(fnmatch(p, d) for d in docs)]
    return bool(rest) and set(unmeasured(rest)) == set(rest)
