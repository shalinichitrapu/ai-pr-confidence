"""Changes the test run can't judge: dependencies, risky code, config files.

Tests and telemetry say nothing about a new third-party package, a call to
eval(), or an edited CI file. These checks read the diff instead.
"""

from __future__ import annotations

import ast
import configparser
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
    "requirements*.in", "*/requirements*.in", "requirements/*.in", "*-requirements*.in", "*/*-requirements*.in",
    "constraints*.txt", "*/constraints*.txt",
)
_REQ_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")
# Requirement lines that aren't "name spec": kept whole, because they're the
# riskiest kind (a git repo, a URL, a local path, another file, a package index).
_RAW = ("-e", "--editable", "-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
        "--extra-index-url", "-f", "--find-links", "git+", "hg+", "svn+", "http://", "https://",
        "file:", "./", "../", "/")

Deps = Dict[Tuple[str, str], str]  # (section, key) -> normalized spec; key includes extras and marker


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_marker(marker: str) -> str:
    """Same marker however it's spaced, quoted or ordered: `a and b` == `b and a`."""
    if re.search(r"\bor\b", marker) or "(" in marker:
        return "".join(marker.split()).replace("'", '"')  # only plain "and" lists are reordered
    # Split on the keyword before removing spaces, so values like "android" stay whole.
    clauses = re.split(r"\s+and\s+", marker.strip())
    return " and ".join(sorted("".join(c.split()).replace("'", '"') for c in clauses if c.strip()))


def _norm_spec(spec: str) -> str:
    """Same version spec however it's spaced or ordered: `>=1,<2` == `<2, >=1`."""
    text = "".join(spec.split())
    return text if text.startswith("@") else ",".join(sorted(p for p in text.split(",") if p))


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
    extras = ",".join(sorted(_norm(e) for e in (m.group(2) or "").strip("[]").split(",") if e.strip()))
    key = _norm(m.group(1)) + (f"[{extras}]" if extras else "") + (f";{_norm_marker(marker)}" if marker.strip() else "")
    return key, _norm_spec(spec)


def _requirements(text: str, section: str = "") -> Deps:
    deps = {}
    for line in text.splitlines():
        parsed = _requirement(line)
        if parsed:
            deps[(section, parsed[0])] = parsed[1]
    return deps


def _poetry(table, section: str) -> Deps:
    deps = {}
    for name, spec in (table or {}).items():
        if _norm(name) != "python":
            value = spec if isinstance(spec, str) else repr(sorted(spec.items()) if isinstance(spec, dict) else spec)
            deps[(section, _norm(name))] = value
    return deps


def _at(doc, *keys) -> dict:
    """doc[k1][k2]..., or {} if any step is missing or isn't a table (`tool = 3`)."""
    for key in keys:
        doc = doc.get(key) if isinstance(doc, dict) else None
    return doc if isinstance(doc, dict) else {}


def _strs(value) -> str:
    return "\n".join(v for v in value if isinstance(v, str)) if isinstance(value, list) else ""


def _pyproject(text: str) -> Deps:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    project = _at(doc, "project")
    deps = _requirements(_strs(project.get("dependencies")), "[project] dependencies")
    for extra, lines in _at(project, "optional-dependencies").items():
        deps.update(_requirements(_strs(lines), f"optional-dependencies.{extra}"))
    for group, items in _at(doc, "dependency-groups").items():  # PEP 735
        deps.update(_requirements(_strs(items), f"dependency-groups.{group}"))
    deps.update(_requirements(_strs(_at(doc, "build-system").get("requires")), "build-system"))
    poetry = _at(doc, "tool", "poetry")
    deps.update(_poetry(poetry.get("dependencies"), "poetry"))
    deps.update(_poetry(poetry.get("dev-dependencies"), "poetry dev"))
    for group, table in _at(poetry, "group").items():
        deps.update(_poetry(_at(table, "dependencies") or None, f"poetry group {group}"))
    return deps


def _setup_cfg(text: str) -> Deps:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}
    deps: Deps = {}
    if parser.has_option("options", "install_requires"):
        deps.update(_requirements(parser.get("options", "install_requires"), "install_requires"))
    if parser.has_option("options", "setup_requires"):
        deps.update(_requirements(parser.get("options", "setup_requires"), "setup_requires"))
    if parser.has_section("options.extras_require"):
        for extra, lines in parser.items("options.extras_require"):
            deps.update(_requirements(lines, f"extras_require.{extra}"))
    return deps


def _strings(node) -> List[str]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _setup_py(text: str) -> Deps:
    """install_requires / setup_requires / extras_require given as literals to setup()."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    deps: Deps = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup":
            for kw in node.keywords:
                if kw.arg in ("install_requires", "setup_requires"):
                    deps.update(_requirements("\n".join(_strings(kw.value)), kw.arg))
                elif kw.arg == "extras_require" and isinstance(kw.value, ast.Dict):
                    for key, value in zip(kw.value.keys, kw.value.values):
                        if isinstance(key, ast.Constant):
                            deps.update(_requirements("\n".join(_strings(value)), f"extras_require.{key.value}"))
    return deps


def is_dependency_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (path.endswith("pyproject.toml") or name in ("setup.py", "setup.cfg")
            or any(fnmatch(path, p) for p in REQUIREMENTS))


def _parse(path: str, text: Optional[str]) -> Deps:
    name = path.rsplit("/", 1)[-1]
    if name == "pyproject.toml":
        return _pyproject(text or "")
    if name == "setup.cfg":
        return _setup_cfg(text or "")
    if name == "setup.py":
        return _setup_py(text or "")
    return _requirements(text or "")


# Settings that decide which tests run or how failures count. Others (markers,
# filterwarnings, log_level, timeout, asyncio_mode) don't stop a test running.
COLLECTION_SETTINGS = {"addopts", "testpaths", "python_files", "python_classes", "python_functions",
                       "norecursedirs", "confcutdir", "xfail_strict", "rootdir", "required_plugins"}


def pytest_settings(path: str, text: str) -> Optional[Dict[str, str]]:
    """The settings in a config file that decide which tests run, or None if pytest doesn't read it.

    A pull request that changes addopts, testpaths or python_files can stop tests
    from running without deleting them.
    """
    name = path.rsplit("/", 1)[-1]
    if name == "pyproject.toml":
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {"(unparsable)": text}
        pytest_table = _at(doc, "tool", "pytest")
        # pytest 9 reads [tool.pytest] directly; older versions [tool.pytest.ini_options].
        table = {**{k: v for k, v in pytest_table.items() if k != "ini_options"}, **_at(pytest_table, "ini_options")}
        return {k: repr(v) for k, v in table.items() if k in COLLECTION_SETTINGS}
    sections = {"pytest.ini": "pytest", "tox.ini": "pytest", "setup.cfg": "tool:pytest"}
    if name not in sections:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {"(unparsable)": text}
    if not parser.has_section(sections[name]):
        return {}
    return {k: v for k, v in parser.items(sections[name]) if k in COLLECTION_SETTINGS}


def all_dependency_changes(files: Iterable[Tuple[str, Optional[str], Optional[str]]]) -> List[str]:
    """Added or changed dependencies across several files: (path, base text, head text).

    Each file, and each table within a pyproject.toml, is compared on its own,
    so one can't hide a change in another. An entry that only moved (the same
    package and spec removed from one place and added in another) isn't new.
    """
    changes, added, removed = [], [], set()
    for path, base_text, head_text in files:
        before, after = _parse(path, base_text), _parse(path, head_text)
        for (section, key), spec in before.items():
            if (section, key) not in after:
                removed.add((key, spec))
        for (section, key), spec in sorted(after.items()):
            where = f"{path} ({section})" if section else path
            if (section, key) not in before:
                added.append((key, spec, where))
            elif spec != before[(section, key)]:
                changes.append(f"{where}: {key} {before[(section, key)] or '(any)'} → {spec or '(any)'}")
    for key, spec, where in added:
        if (key, spec) in removed:
            removed.discard((key, spec))  # moved, not added
            continue
        changes.append(f"{where}: added {key} {spec}".rstrip())
    return sorted(changes)


def dependency_changes(path: str, base_text: Optional[str], head_text: Optional[str]) -> List[str]:
    """Human-readable added or changed dependencies in one file."""
    return all_dependency_changes([(path, base_text, head_text)])


# ---------------------------------------------------------------- risky code

# The name must *end* in a secret word: API_TOKEN and db_password, not TOKEN_URL or TOKENIZER.
# The secret word as a whole part of the name (db_password_prod, API_KEY_LIVE), but not
# a name that only describes one (TOKEN_URL, PASSWORD_RESET_TEMPLATE, TOKENIZER).
SECRET_NAME = re.compile(r"(?:^|_|(?<=[a-z])(?=[A-Z]))(password|passwd|pwd|secret|api_?key|access_?key|secret_?key"
                         r"|token|private_?key)\d*(?!_?(url|uri|header|template|name|path|file|dir|type|env|var"
                         r"|len|length|field|label|prefix|endpoint|timeout|expiry|expires|ttl|count|reset))"
                         r"(?![a-z])", re.IGNORECASE)
NOT_A_SECRET = re.compile(r"^(https?://|/)")  # URLs and paths
ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")  # APP_PASSWORD: the name of a variable, not a value
KNOWN_SECRET = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$|^(sk|rk)_(live|test)_|^sk-[A-Za-z0-9_-]{20,}|^gh[pousr]_"
                          r"|^xox[abpr]-|^AIza")


def _looks_like_secret(value: str) -> bool:
    if KNOWN_SECRET.match(value):
        return True
    if NOT_A_SECRET.match(value) or ENV_VAR_NAME.match(value):
        return False
    # Real secrets mix letters and digits, or are long unbroken strings; identifiers
    # like "bert-base-uncased" or "my.module.name" are neither.
    if any(c.isdigit() for c in value) and any(c.isalpha() for c in value):
        return True
    return len(value) >= 12 and not any(c in value for c in "-./")


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
    """The dotted name a call resolves to, seeing through aliases,
    `getattr(os, "system")(...)` and `__import__("pickle").loads(...)`."""
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(aliases.get(target.id, target.id))
    elif isinstance(target, ast.Call) and isinstance(target.func, (ast.Name, ast.Attribute)):
        args = [a.value for a in target.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        func_name = target.func.id if isinstance(target.func, ast.Name) else target.func.attr
        if func_name in ("__import__", "import_module") and args:
            parts.append(args[0])
        elif (func_name == "getattr" and len(target.args) >= 2 and isinstance(target.args[0], ast.Name)
              and isinstance(target.args[1], ast.Constant)):
            parts.append(f"{aliases.get(target.args[0].id, target.args[0].id)}.{target.args[1].value}")
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
        if name.startswith("subprocess.") and shell is not None and not _is_const(shell, False):
            if _is_const(shell, True):
                return f"{name}(shell=True) runs a shell command", at | {shell.lineno}
            return f"{name}(shell={ast.unparse(shell)}) may run a shell command", at | {shell.lineno}
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
    pairs = []  # (name, value node) for assignments, keyword arguments and dict entries
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        pairs = [(t.id if isinstance(t, ast.Name) else getattr(t, "attr", ""), node.value) for t in targets]
    elif isinstance(node, ast.keyword) and node.arg:
        pairs = [(node.arg, node.value)]
    elif isinstance(node, ast.Dict):
        pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    for name, value in pairs:
        if (name and SECRET_NAME.search(name) and isinstance(value, ast.Constant) and isinstance(value.value, str)
                and len(value.value) >= 8 and " " not in value.value and _looks_like_secret(value.value)):
            return f"hard-coded secret in {name}", {value.lineno}
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


# Data next to tests or templates is usually loaded by the tests themselves.
EXERCISED = ("tests/*", "test/*", "*/tests/*", "*/fixtures/*", "fixtures/*", "*/testdata/*", "testdata/*")


def unmeasured(paths: Iterable[str]) -> List[str]:
    """Changed CI, deployment and configuration files that tests can't exercise."""
    return sorted(p for p in paths
                  if not is_dependency_file(p) and any(fnmatch(p, pattern) for pattern in INFRA)
                  and not any(fnmatch(p, pattern) for pattern in EXERCISED))


def config_only(paths: Iterable[str]) -> bool:
    """True if every changed file is configuration/infrastructure (docs and dependency files aside)."""
    docs = ("*.md", "*.rst", "README*", "CHANGELOG*", "LICENSE*", "docs/*", ".gitignore")
    rest = [p for p in paths if not is_dependency_file(p) and not any(fnmatch(p, d) for d in docs)]
    return bool(rest) and set(unmeasured(rest)) == set(rest)
