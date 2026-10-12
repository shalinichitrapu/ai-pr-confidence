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


def config_repo(tmp_path, base_toml, change_toml=None, base_extra=None):
    """main has base_toml; branch "change" adds untested code (and optionally new settings)."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    (repo / "pyproject.toml").write_text(base_toml)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "change")
    (repo / "app.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    if change_toml is not None:
        (repo / "pyproject.toml").write_text(change_toml)
    git(repo, "commit", "-qam", "change")
    if base_extra is not None:
        git(repo, "checkout", "-q", "main")
        (repo / "pyproject.toml").write_text(base_extra)
        git(repo, "commit", "-qam", "tighten settings")
    return repo


def run_cli(repo, out):
    code = cli.main(["--repo", str(repo), "--base", "main", "--head", "change", "--no-llm", "--no-memory",
                     "--out", str(out)])
    return code, (out / "report.md").read_text()


def test_settings_come_from_the_tip_of_the_base_branch(tmp_path):
    # The change started from a commit with min_score 0; main has since raised it to 90.
    repo = config_repo(tmp_path, '[tool.confidence]\nsource = "app"\nmin_score = 0\n',
                       base_extra='[tool.confidence]\nsource = "app"\nmin_score = 90\n')
    code, _ = run_cli(repo, tmp_path / "out")
    assert code == 1


def test_a_mistake_in_the_changes_own_settings_fails_the_run(tmp_path):
    repo = config_repo(tmp_path, '[tool.confidence]\nsource = "app"\n',
                       change_toml='[tool.confidence]\nsource = "app"\nmin_scor = 60\n')
    code, report = run_cli(repo, tmp_path / "out")
    assert code == 2
    assert "This change's [tool.confidence] has a mistake" in report and "min_scor" in report


def test_a_mistake_on_the_base_branch_warns_and_uses_defaults(tmp_path):
    # The change fixes the typo; it must still be scored rather than blocked.
    repo = config_repo(tmp_path, "[tool.confidence]\nsorce = 'x'\n", change_toml='[tool.confidence]\nsource = "."\n')
    code, report = run_cli(repo, tmp_path / "out")
    assert code == 0
    assert "has a mistake, so defaults were used" in report


@pytest.mark.parametrize("text", [
    "[tool.confidence]\nbands = 5\n",
    "[tool.confidence]\nweights = [1]\n",
    "[tool]\nconfidence = 1\n",
    "tool = 1\n",
])
def test_values_of_the_wrong_kind_are_config_errors(text):
    with pytest.raises(ConfigError, match="must be a table"):
        parse(text)


def test_ignore_example_matches_migrations_folders_exactly():
    cfg = parse('[tool.confidence]\nignore = ["migrations/*", "*/migrations/*"]\n')
    assert cfg.ignored("migrations/0001.py") and cfg.ignored("app/migrations/0001.py")
    assert not cfg.ignored("nomigrations/x.py") and not cfg.ignored("data_migrations/x.py")


def test_inheriting_a_broken_base_table_doesnt_block_the_pull_request(tmp_path):
    # main's table has a typo; the change only touches app.py.
    repo = config_repo(tmp_path, "[tool.confidence]\nsorce = 'x'\n")
    code, report = run_cli(repo, tmp_path / "out")
    assert code == 0
    assert "has a mistake, so defaults were used" in report
    assert "This change's [tool.confidence] has a mistake" not in report


def test_an_already_invalid_pyproject_doesnt_fail_an_unrelated_change():
    from confidence_agent.config import raw_table
    assert raw_table("[broken\nname = 1\n") is raw_table("[broken\nname = 2\n")
    assert raw_table("[tool.confidence]\nsource = 'a'\n") != raw_table("[broken\n")
