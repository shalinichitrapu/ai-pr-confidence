"""[tool.confidence] settings: parsing, validation, and how they change scoring."""

import json
import re
import subprocess

import pytest

from confidence_agent import cli
from confidence_agent.config import Config, ConfigError, Tier, parse
from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import Signals, compute

FULL = """
[tool.confidence]
source = "app"
tests = "checks"
min_score = 60
ignore = ["*/migrations/*", "*_pb2.py"]
expected_errors = ["pay.py:refund: TimeoutError"]

[tool.confidence.bands]
high = 90
medium = 70

[tool.confidence.thresholds]
large_diff_lines = 1000
latency_ratio = 3

[tool.confidence.weights]
error_logs = [30, 5, 40]
size = [0, 0, 0]
"""


def test_missing_table_gives_defaults():
    assert parse("") == Config()
    assert parse("[project]\nname = 'x'\n") == Config()


def test_full_table():
    cfg = parse(FULL)
    assert (cfg.source, cfg.tests, cfg.min_score) == ("app", "checks", 60)
    assert cfg.ignore == ["*/migrations/*", "*_pb2.py"]
    assert (cfg.bands.high, cfg.bands.medium) == (90, 70)
    assert cfg.thresholds.large_diff_lines == 1000 and cfg.thresholds.latency_ratio == 3
    assert cfg.thresholds.metric_ratio == 2.0  # untouched defaults stay
    assert cfg.weights.error_logs == Tier(30, 5, 40)
    assert cfg.ignored("app/migrations/0001.py") and cfg.ignored("api_pb2.py") and not cfg.ignored("app/a.py")
    assert cfg.expected("app pay.py:refund: TimeoutError")


@pytest.mark.parametrize("text, message", [
    ("[tool.confidence]\nsorce = 'x'\n", "unknown key(s) in [tool.confidence]: sorce"),
    ("[tool.confidence.weights]\nlogs = [1, 2, 3]\n", "unknown key(s) in [tool.confidence.weights]: logs"),
    ("[tool.confidence.weights]\nerror_logs = 20\n", "must be [first, each, maximum]"),
    ("[tool.confidence.weights]\nerror_logs = [20, -1, 25]\n", "must be [first, each, maximum]"),
    ("[tool.confidence.bands]\nhigh = 50\nmedium = 60\n", "medium <= high"),
    ("[tool.confidence.thresholds]\nlatency_ratio = 'fast'\n", "must be a number"),
    ("[tool.confidence]\nignore = 'x'\n", "must be a list of strings"),
    ("[tool.confidence\n", "not valid TOML"),
])
def test_mistakes_are_reported_clearly(text, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse(text)


def test_tier_points():
    assert [Tier(20, 5, 25).points(n) for n in range(4)] == [0, 20, 25, 25]


def test_weights_bands_and_thresholds_change_the_score():
    cfg = parse(FULL)
    s = Signals(tests_total=1, new_error_logs={"a": 1}, code_diff_lines=500)
    default, custom = score(s), score(s, cfg)
    assert (default.value, default.band) == (75, "Medium")  # -20 logs, -5 size
    assert (custom.value, custom.band) == (70, "Medium")  # -30 logs, size off, medium starts at 70
    assert all(d[0] != "Size" for d in custom.deductions)


def test_ignored_files_are_not_scored():
    diff = Diff(added_lines={"app/migrations/0002.py": {1, 2}, "app/a.py": {1}},
                files_changed=["app/migrations/0002.py", "app/a.py"],
                file_lines={"app/migrations/0002.py": 900, "app/a.py": 1},
                added_text={"app/migrations/0002.py": ["x = 1  # pragma: no cover"]})
    cov = {"app/a.py": {"executed_lines": [1], "missing_lines": []}}
    s = compute(RunResult("b"), RunResult("h", coverage=cov), diff, "tests", "app", parse(FULL))
    assert s.ignored_files == ["app/migrations/0002.py"]
    assert s.changed_code_lines == 1 and s.uncovered_changes == {}
    assert s.code_diff_lines == 1
    assert s.hidden_code == {}


def test_expected_errors_are_shown_not_scored():
    t = "tests/test_pay.py::test_refund"
    log = {"level": "ERROR", "logger": "app", "template": "x", "message": "x", "exc_type": "TimeoutError",
           "location": "pay.py:refund", "test": t}
    base = RunResult("b", tests={"a": "passed"}, node_ids=[t])
    head = RunResult("h", tests={"a": "passed"}, node_ids=[t], logs=[log])
    s = compute(base, head, Diff(), "tests", ".", parse(FULL))
    assert s.new_error_logs == {}
    assert s.expected_errors == {"app pay.py:refund: TimeoutError": 1}
    assert score(s).value == 100


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_settings_come_from_the_base_commit_not_the_pull_request(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    (repo / "pyproject.toml").write_text('[tool.confidence]\nsource = "app"\n')
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    # The change adds untested code and tries to exclude it from scoring.
    (repo / "app.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    (repo / "pyproject.toml").write_text('[tool.confidence]\nsource = "app"\nignore = ["app.py"]\n')
    git(repo, "commit", "-qam", "change")
    out = tmp_path / "out"
    code = cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--no-llm",
                     "--no-memory", "--out", str(out)])
    result = json.loads((out / "result.json").read_text())
    assert code == 0
    assert result["signals"]["ignored_files"] == []
    assert any(d["signal"] == "Coverage" for d in result["deductions"])


def test_a_broken_config_at_the_base_stops_the_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.confidence]\nsorce = 'x'\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "checkout", "-qb", "change")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change")
    assert cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--no-llm"]) == 2
