"""A test that fails once and passes on a rerun is flaky, not a new failure."""

import json
import subprocess
from pathlib import Path

from confidence_agent import cli
from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult, rerun_tests
from confidence_agent.scoring import score
from confidence_agent.signals import compute

# Fails the first time it runs, passes after that (state kept in a file outside the repo).
FLAKY_TEST = '''
import os
from pathlib import Path


def test_sometimes():
    marker = Path(os.environ["FLAKY_MARKER"])
    if not marker.exists():
        marker.write_text("ran")
        assert False, "first run fails"
'''


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_rerun_reports_the_second_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_MARKER", str(tmp_path / "marker"))
    (tmp_path / "marker").write_text("already ran once")
    project = write(tmp_path / "proj", {
        "tests/test_flaky.py": FLAKY_TEST,
        "tests/test_broken.py": "def test_broken():\n    assert False\n",
    })
    outcomes = rerun_tests(project, ["tests.test_flaky::test_sometimes", "tests.test_broken::test_broken"], "tests")
    assert outcomes == {"tests.test_flaky::test_sometimes": "passed", "tests.test_broken::test_broken": "failed"}


def test_flaky_tests_are_not_new_failures():
    base = RunResult("b", tests={"t::flaky": "passed", "t::broken": "passed"})
    head = RunResult("h", tests={"t::flaky": "failed", "t::broken": "failed"},
                     rerun={"t::flaky": "passed", "t::broken": "failed"})
    s = compute(base, head, Diff(), "tests")
    assert s.tests_flaky == ["t::flaky"]
    assert s.tests_newly_failing == ["t::broken"]
    assert s.tests_failed == ["t::broken"]


def test_flaky_only_costs_5():
    base = RunResult("b", tests={"t::flaky": "passed"})
    head = RunResult("h", tests={"t::flaky": "failed"}, rerun={"t::flaky": "passed"})
    r = score(compute(base, head, Diff(), "tests"))
    assert (r.value, r.band) == (95, "High")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_end_to_end_flaky_test_is_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_MARKER", str(tmp_path / "marker"))
    repo = write(tmp_path / "repo", {"tests/test_flaky.py": "def test_sometimes():\n    pass\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    write(repo, {"tests/test_flaky.py": FLAKY_TEST})
    git(repo, "commit", "-qam", "change")
    out = tmp_path / "out"
    cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--no-llm", "--no-memory",
              "--out", str(out)])
    result = json.loads((out / "result.json").read_text())
    assert result["signals"]["tests_flaky"] == ["tests.test_flaky::test_sometimes"]
    assert result["signals"]["tests_newly_failing"] == []
    assert result["score"] == 95


def test_order_dependent_failure_is_not_called_flaky(tmp_path):
    # The change shares a mutable default between calls: test_b fails only after test_a runs.
    repo = write(tmp_path / "repo", {
        "m.py": "DEFAULTS = {'limit': 10}\n\ndef settings(**kw):\n    s = dict(DEFAULTS)\n    s.update(kw)\n    return s\n",
        "tests/test_m.py": "from m import settings\n\ndef test_a():\n    assert settings(limit=1)['limit'] == 1\n\n"
                           "def test_b_default():\n    assert settings()['limit'] == 10\n",
    })
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    (repo / "m.py").write_text((repo / "m.py").read_text().replace("dict(DEFAULTS)", "DEFAULTS"))
    git(repo, "commit", "-qam", "change")
    out = tmp_path / "out"
    cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--source", "m", "--no-llm",
              "--no-memory", "--out", str(out)])
    result = json.loads((out / "result.json").read_text())
    assert result["signals"]["tests_flaky"] == []
    assert result["signals"]["tests_newly_failing"] == ["tests.test_m::test_b_default"]
    assert result["band"] == "Low"
