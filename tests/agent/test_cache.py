"""The base run is cached by everything that could change its results."""

import json
import subprocess

from confidence_agent import cache, cli
from confidence_agent.runner import RunResult


def test_round_trip(tmp_path):
    run = RunResult("base", tests={"t::a": "passed"}, node_ids=["t.py::a"], memory={"t.py::a": 10},
                    coverage={"a.py": {"executed_lines": [1]}})
    cache.save(tmp_path, "k", run)
    loaded = cache.load(tmp_path, "k")
    assert loaded.from_cache
    loaded.from_cache = False
    assert loaded == run


def test_missing_or_corrupt_entries_are_misses(tmp_path):
    assert cache.load(tmp_path, "nope") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert cache.load(tmp_path, "bad") is None


def test_key_changes_with_every_input():
    k = cache.key("abc", "app", "tests", True)
    assert k == cache.key("abc", "app", "tests", True)
    assert len({k, cache.key("abd", "app", "tests", True), cache.key("abc", "lib", "tests", True),
                cache.key("abc", "app", "spec", True), cache.key("abc", "app", "tests", False)}) == 5


def test_old_entries_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "KEEP", 3)
    for i in range(5):
        cache.save(tmp_path, f"k{i}", RunResult("base", tests={"t": "passed"}))
    assert len(list(tmp_path.glob("*.json"))) == 3


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_second_pull_request_against_the_same_base_reuses_its_run(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("def f():\n    return 1\n")
    (repo / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    for branch in ("one", "two"):
        git(repo, "checkout", "-q", "-b", branch, "main")
        (repo / f"{branch}.txt").write_text(branch)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", branch)
    args = ["--repo", str(repo), "--base", "main", "--source", "app", "--no-llm", "--no-memory",
            "--cache-dir", str(tmp_path / "cache")]
    cli.main(args + ["--head", "one", "--out", str(tmp_path / "o1")])
    assert "using cached results" not in capsys.readouterr().out
    cli.main(args + ["--head", "two", "--out", str(tmp_path / "o2")])
    assert "using cached results" in capsys.readouterr().out
    first = json.loads((tmp_path / "o1" / "result.json").read_text())
    second = json.loads((tmp_path / "o2" / "result.json").read_text())
    assert first["score"] == second["score"] == 100
    cli.main(args + ["--head", "two", "--no-cache", "--out", str(tmp_path / "o3")])
    assert "using cached results" not in capsys.readouterr().out


def test_a_base_run_with_failures_is_not_cached(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text("import os\n\ndef f():\n    return 2 if os.environ.get('FLAKE') else 1\n")
    (repo / "tests" / "test_app.py").write_text("from app import f\n\ndef test_f():\n    assert f() == 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    for branch, body in (("one", "1  # unrelated tweak"), ("two", "3")):  # "two" really breaks f()
        git(repo, "checkout", "-q", "-b", branch, "main")
        (repo / "app.py").write_text(f"import os\n\ndef f():\n    return 2 if os.environ.get('FLAKE') else {body}\n")
        git(repo, "commit", "-qam", branch)
    args = ["--repo", str(repo), "--base", "main", "--source", "app", "--no-llm", "--no-memory", "--no-mutation",
            "--cache-dir", str(tmp_path / "cache")]
    monkeypatch.setenv("FLAKE", "1")  # the base run happens to fail
    cli.main(args + ["--head", "one", "--out", str(tmp_path / "o1")])
    monkeypatch.delenv("FLAKE")
    cli.main(args + ["--head", "two", "--out", str(tmp_path / "o2")])
    second = json.loads((tmp_path / "o2" / "result.json").read_text())
    assert second["signals"]["tests_newly_failing"] == ["tests.test_app::test_f"]
    assert second["band"] == "Low"


def test_latency_is_not_compared_against_a_cached_base():
    from confidence_agent.gitutil import Diff
    from confidence_agent.scoring import score
    from confidence_agent.signals import compute
    span = lambda ms: {"name": "op", "duration_ms": ms, "status": "UNSET", "events": [], "test": "t.py::a"}
    base = RunResult("base", tests={"t::a": "passed"}, node_ids=["t.py::a"], spans=[span(1.0)], otel_enabled=True,
                     from_cache=True)
    head = RunResult("head", tests={"t::a": "passed"}, node_ids=["t.py::a"], spans=[span(50.0)], otel_enabled=True)
    s = compute(base, head, Diff(), "tests")
    assert s.latency_regressions == {} and "cache" in s.latency_skipped
    assert "No latency regressions in traced operations" not in score(s).passed_checks
