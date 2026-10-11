"""Calibration: score past changes and compare with what happened to them."""

import subprocess
from pathlib import Path

from confidence_agent import calibrate
from confidence_agent.calibrate import Change, report


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, files: dict):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def project(tmp_path) -> Path:
    repo = tmp_path / "repo"
    write(repo, {"app.py": "def f():\n    return 1\n",
                 "tests/test_app.py": "from app import f\n\ndef test_f():\n    assert f() == 1\n"})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "start")
    return repo


def merge(repo, branch, files, message):
    git(repo, "checkout", "-q", "-b", branch, "main")
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge {branch}")
    return git(repo, "rev-parse", "HEAD")


def test_reverted_merge_is_marked_bad_and_scored(tmp_path):
    repo = project(tmp_path)
    merge(repo, "good", {"app.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n",
                         "tests/test_g.py": "from app import g\n\ndef test_g():\n    assert g() == 2\n"}, "Add g")
    bad = merge(repo, "bad", {"app.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n\n\n"
                                        "def h(x):\n    if x:\n        return 3\n    return 4\n"}, "Add h")
    git(repo, "revert", "--no-edit", "-m", "1", bad)
    out = tmp_path / "out"
    assert calibrate.main(["--repo", str(repo), "--limit", "10", "--source", "app", "--out", str(out)]) == 0
    rows = (out / "calibration.csv").read_text().splitlines()[1:]
    by_subject = {r.split(",")[1]: r for r in rows}
    assert by_subject["Merge bad"].split(",")[5] == "bad"
    assert by_subject["Merge good"].split(",")[5] == "good"
    assert ",100,High," in by_subject["Merge good"]
    assert "Coverage" in by_subject["Merge bad"]  # h() is untested
    md = (out / "calibration.md").read_text()
    assert "2 changes scored, 1 later reverted or marked bad (50%)" in md


def test_squash_merged_history_uses_each_commit(tmp_path):
    repo = project(tmp_path)
    write(repo, {"notes.md": "a\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Docs")
    found = calibrate.changes(repo, "main", 10)
    assert [c.subject for c in found] == ["Docs"]  # the root commit has no parent to compare with


def test_outcomes_file_overrides(tmp_path):
    repo = project(tmp_path)
    sha = merge(repo, "x", {"notes.md": "a\n"}, "Notes")
    outcomes = tmp_path / "outcomes.csv"
    outcomes.write_text(f"{sha[:10]},bad\n")
    out = tmp_path / "out"
    calibrate.main(["--repo", str(repo), "--outcomes", str(outcomes), "--out", str(out)])
    assert ",bad,from --outcomes" in (out / "calibration.csv").read_text()


def test_report_lift():
    items = [Change("a" * 40, "", "", "one", score=95, band="High", signals=[], outcome="good"),
             Change("b" * 40, "", "", "two", score=70, band="Medium", signals=["Coverage"], outcome="bad"),
             Change("c" * 40, "", "", "three", score=70, band="Medium", signals=["Coverage"], outcome="good"),
             Change("d" * 40, "", "", "four", score=55, band="Low", signals=["Tests"], outcome="bad")]
    md = report(items)
    assert "| Medium | 2 | 1 | 50% |" in md
    assert "| Coverage | 2 | 1 | 1.0 |" in md  # 50% vs 50% overall
    assert "| Tests | 1 | 1 | 2.0 |" in md
