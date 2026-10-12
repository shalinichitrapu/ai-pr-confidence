"""Run a project's test suite with coverage and telemetry capture."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

PLUGIN_MODULE = "confidence_pytest_plugin"
# Plugins that shuffle test order. The before, after and rerun runs must use the
# same order, or an order-dependent failure could pass on the rerun by chance.
NO_SHUFFLE = ["-p", "no:randomly", "-p", "no:random_order"]


@dataclass
class RunResult:
    label: str
    tests: Dict[str, str] = field(default_factory=dict)  # test id -> passed/failed/error/skipped
    test_durations: Dict[str, float] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)  # coverage.py JSON "files"
    spans: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    metrics: List[Dict[str, Any]] = field(default_factory=list)  # OTel metric points, tagged by test
    memory: Dict[str, int] = field(default_factory=dict)  # test id -> peak bytes allocated
    node_ids: List[str] = field(default_factory=list)  # pytest ids that metrics and memory use
    otel_enabled: bool = False
    otel_metrics_enabled: bool = False
    # Files the tests imported from the user's repository instead of this checkout.
    outside_imports: List[str] = field(default_factory=list)
    rerun: Dict[str, str] = field(default_factory=dict)  # test id -> outcome when run again
    mutation: Any = None  # mutate.MutationResult for the head run, if mutation testing ran
    from_cache: bool = False  # loaded from the base-run cache rather than run now
    exit_code: int = 0
    output_tail: str = ""

    @property
    def failed(self) -> List[str]:
        return sorted(t for t, o in self.tests.items() if o in ("failed", "error"))


def _plugin_dir() -> Path:
    """Copy the plugin into its own folder so only it lands on PYTHONPATH."""
    d = Path(tempfile.mkdtemp(prefix="confidence-plugin-"))
    shutil.copy(Path(__file__).with_name("pytest_plugin.py"), d / f"{PLUGIN_MODULE}.py")
    return d


def _parse_junit(path: Path) -> tuple:
    tests: Dict[str, str] = {}
    durations: Dict[str, float] = {}
    if not path.exists():
        return tests, durations
    for case in ET.parse(path).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": "failed"}.get(child.tag, child.tag)
        tests[test_id] = outcome
        durations[test_id] = float(case.get("time") or 0)
    return tests, durations


def _import_paths(workdir: Path) -> List[str]:
    """Folders that must win over any installed copy of the project.

    An editable install (pip install -e) points Python at the user's own
    checkout. Putting this checkout first makes the tests import the version
    being scored. A src/ layout needs src/ on the path as well.
    """
    paths = [str(workdir)]
    if (workdir / "src").is_dir():
        paths.insert(0, str(workdir / "src"))
    return paths


def junit_id(node_id: str) -> str:
    """pytest's "tests/test_a.py::TestX::test_f[x]" -> JUnit's "tests.test_a.TestX::test_f[x]".

    Parametrize ids may contain "::" themselves, so only the part before "[" is split.
    """
    base, bracket, params = node_id.partition("[")
    path, *rest = base.split("::")
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path
    if not rest:
        return node_id
    return "::".join([".".join([module, *rest[:-1]]), rest[-1]]) + bracket + params


# GitHub credentials the project's tests must not see. In CI the job's token can
# label and comment on the pull request, and the pull request's own test code
# could use it, e.g. to acknowledge its own deductions. Other variables (such as
# a database password integration tests need) are left alone.
SECRET_ENV = re.compile(r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN|ACTIONS_RUNTIME_TOKEN"
                        r"|ACTIONS_ID_TOKEN_REQUEST_TOKEN|ACTIONS_ID_TOKEN_REQUEST_URL|ACTIONS_CACHE_URL"
                        # Files that set later steps' environment, outputs and the job summary.
                        r"|GITHUB_ENV|GITHUB_OUTPUT|GITHUB_PATH|GITHUB_STATE|GITHUB_STEP_SUMMARY)$")

# The agent's own coverage settings. The project's (and especially the pull
# request's) .coveragerc / [tool.coverage] could omit or exclude the code being
# scored, or turn on parallel mode or dynamic contexts that break collection.
COVERAGE_RC = """[run]
parallel = False
branch = False
"""


def test_env(workdir: Path, extra_paths: List[str] = ()) -> Dict[str, str]:
    """The environment the project's tests run in: no credentials, this checkout first on the path."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (*extra_paths, *_import_paths(workdir), os.environ.get("PYTHONPATH", "")) if p
    )
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("COVERAGE_RCFILE", None)
    return env


def pytest_flags(scratch: Path) -> List[str]:
    """Options every pytest run gets. The cache goes to scratch rather than being turned off,
    so --lf/--ff in the project's addopts still parse (they just find an empty cache)."""
    return ["-o", f"cache_dir={scratch / 'pytest-cache'}", "--continue-on-collection-errors"]


def optional_flags() -> List[str]:
    """Flags that keep runs comparable but that some setups reject (e.g. -n without xdist loaded)."""
    flags = list(NO_SHUFFLE)
    if importlib.util.find_spec("xdist") is not None:
        # Parallel workers are separate processes that coverage and the plugin don't see.
        flags += ["-n", "0"]
    return flags


def run_pytest(cmd: List[str], optional: List[str], **kw) -> subprocess.CompletedProcess:
    """Run pytest with the optional flags; if pytest rejects its arguments (exit 4), retry without them.

    That covers PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 or "-p no:xdist" in the project's
    config, where "-n 0" is an unknown option.
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    proc = subprocess.run(cmd + optional, **kw)
    if proc.returncode == 4 and "-n" in optional:
        # Only "-n 0" can be unknown; keep the no-shuffle flags so the order stays fixed.
        i = optional.index("-n")
        proc = subprocess.run(cmd + optional[:i] + optional[i + 2:], **kw)
    return proc


def rerun_tests(workdir: Path, test_ids: List[str], tests_path: str) -> Dict[str, str]:
    """Run the same tests again, in the same order, and return the outcomes of test_ids.

    The whole set runs again, not just the failing tests: a failure that depends
    on test order or on state another test leaves behind passes when the test
    runs alone, and must not be mistaken for flakiness. It runs plainly (no
    coverage or telemetry), and only when something newly failed.
    """
    if not test_ids:
        return {}
    scratch = Path(tempfile.mkdtemp(prefix="confidence-rerun-"))
    try:
        junit = scratch / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", tests_path, "-q",
               f"--junitxml={junit}", "-o", "junit_family=xunit2", *pytest_flags(scratch)]
        run_pytest(cmd, optional_flags(), cwd=workdir, env=test_env(workdir))
        outcomes, _ = _parse_junit(junit)
        return {t: outcomes[t] for t in test_ids if t in outcomes}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_tests(workdir: Path, label: str, source: str, tests_path: str, track_memory: bool = True,
              repo: Path = None) -> RunResult:
    scratch = Path(tempfile.mkdtemp(prefix=f"confidence-{label}-"))
    plugin_dir = _plugin_dir()
    try:
        junit = scratch / "junit.xml"
        telemetry = scratch / "telemetry.json"
        cov_json = scratch / "coverage.json"
        rcfile = scratch / "coveragerc"
        rcfile.write_text(COVERAGE_RC)

        env = test_env(workdir, [str(plugin_dir)])
        env["CONFIDENCE_WORKDIR"] = str(workdir.resolve())
        if repo is not None:
            env["CONFIDENCE_REPO"] = str(Path(repo).resolve())
        env["CONFIDENCE_TELEMETRY_OUT"] = str(telemetry)
        env["COVERAGE_FILE"] = str(scratch / ".coverage")
        env["CONFIDENCE_MEMORY"] = "1" if track_memory else "0"

        cmd = [
            sys.executable, "-m", "coverage", "run", f"--rcfile={rcfile}", f"--source={source}",
            "-m", "pytest", tests_path, "-q", "-p", PLUGIN_MODULE,
            f"--junitxml={junit}", "-o", "junit_family=xunit2", *pytest_flags(scratch),
        ]
        proc = run_pytest(cmd, optional_flags(), cwd=workdir, env=env)
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", f"--rcfile={rcfile}", "-q", "--show-contexts",
             "-o", str(cov_json)],
            cwd=workdir, env=env, capture_output=True, text=True, errors="replace",
        )

        result = RunResult(label=label, exit_code=proc.returncode)
        result.output_tail = (proc.stdout + proc.stderr)[-3000:]
        result.tests, result.test_durations = _parse_junit(junit)
        if cov_json.exists():
            result.coverage = json.loads(cov_json.read_text()).get("files", {})
        if telemetry.exists():
            data = json.loads(telemetry.read_text())
            result.spans, result.logs = data["spans"], data["logs"]
            result.metrics, result.memory = data.get("metrics", []), data.get("memory", {})
            result.node_ids = data.get("test_ids", [])
            result.otel_enabled = data.get("otel", False)
            result.otel_metrics_enabled = data.get("otel_metrics", False)
            result.outside_imports = data.get("outside_imports", [])
        return result
    finally:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
