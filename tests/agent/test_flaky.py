"""A test that fails once and passes on a rerun is flaky, not a new failure."""

import json
import subprocess
from pathlib import Path

from confidence_agent import cli
from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult, _node_id, rerun_tests
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


def test_junit_ids_map_back_to_pytest_node_ids(tmp_path):
    write(tmp_path, {"tests/test_a.py": "", "tests/sub/test_b.py": ""})
    assert _node_id(tmp_path, "tests.test_a::test_x") == "tests/test_a.py::test_x"
    assert _node_id(tmp_path, "tests.test_a.TestThing::test_y[1]") == "tests/test_a.py::TestThing::test_y[1]"
    assert _node_id(tmp_path, "tests.sub.test_b::test_z") == "tests/sub/test_b.py::test_z"


def test_rerun_reports_the_second_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKY_MARKER", str(tmp_path / "marker"))
    (tmp_path / "marker").write_text("already ran once")
    project = write(tmp_path / "proj", {
        "tests/test_flaky.py": FLAKY_TEST,
        "tests/test_broken.py": "def test_broken():\n    assert False\n",
    })
    outcomes = rerun_tests(project, ["tests.test_flaky::test_sometimes", "tests.test_broken::test_broken"])
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
