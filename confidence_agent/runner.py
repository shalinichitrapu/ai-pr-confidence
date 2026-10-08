"""Run a project's test suite with coverage and telemetry capture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

PLUGIN_MODULE = "confidence_pytest_plugin"


@dataclass
class RunResult:
    label: str
    tests: Dict[str, str] = field(default_factory=dict)  # test id -> passed/failed/error/skipped
    test_durations: Dict[str, float] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)  # coverage.py JSON "files"
    spans: List[Dict[str, Any]] = field(default_factory=list)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    otel_enabled: bool = False
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


def run_tests(workdir: Path, label: str, source: str, tests_path: str) -> RunResult:
    scratch = Path(tempfile.mkdtemp(prefix=f"confidence-{label}-"))
    plugin_dir = _plugin_dir()
    junit = scratch / "junit.xml"
    telemetry = scratch / "telemetry.json"
    cov_json = scratch / "coverage.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(plugin_dir), env.get("PYTHONPATH", "")) if p
    )
    env["CONFIDENCE_TELEMETRY_OUT"] = str(telemetry)
    env["COVERAGE_FILE"] = str(scratch / ".coverage")
    env.pop("PYTEST_ADDOPTS", None)

    cmd = [
        sys.executable, "-m", "coverage", "run", f"--source={source}",
        "-m", "pytest", tests_path, "-q", "-p", PLUGIN_MODULE,
        f"--junitxml={junit}", "-o", "junit_family=xunit2", "-p", "no:cacheprovider",
    ]
    proc = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True)
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-q", "-o", str(cov_json)],
        cwd=workdir, env=env, capture_output=True, text=True,
    )

    result = RunResult(label=label, exit_code=proc.returncode)
    result.output_tail = (proc.stdout + proc.stderr)[-3000:]
    result.tests, result.test_durations = _parse_junit(junit)
    if cov_json.exists():
        result.coverage = json.loads(cov_json.read_text()).get("files", {})
    if telemetry.exists():
        data = json.loads(telemetry.read_text())
        result.spans, result.logs = data["spans"], data["logs"]
        result.otel_enabled = data.get("otel", False)

    shutil.rmtree(plugin_dir, ignore_errors=True)
    shutil.rmtree(scratch, ignore_errors=True)
    return result
