"""Parsing `git diff` into changed files and added line numbers."""

import subprocess
from pathlib import Path

import pytest

from confidence_agent.gitutil import read_diff


def git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    return tmp_path


def commit(repo: Path, files: dict, remove=(), message="c"):
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
    for rel in remove:
        git(repo, "rm", "-q", rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def test_modified_and_new_files(repo):
    commit(repo, {"a.py": "1\n2\n3\n"})
    commit(repo, {"a.py": "1\nTWO\n3\n4\n", "b.py": "x\ny\n"})
    d = read_diff(repo, "HEAD~1", "HEAD")
    assert d.added_lines == {"a.py": {2, 4}, "b.py": {1, 2}}
    assert sorted(d.files_changed) == ["a.py", "b.py"]
    assert (d.lines_added, d.lines_removed, d.size) == (4, 1, 5)


def test_deleted_file_has_no_added_lines(repo):
    commit(repo, {"a.py": "1\n", "gone.py": "1\n2\n"})
    commit(repo, {}, remove=["gone.py"])
    d = read_diff(repo, "HEAD~1", "HEAD")
    assert d.added_lines == {}
    assert d.files_changed == ["gone.py"]
    assert d.lines_removed == 2


def test_binary_files_are_listed_but_not_counted(repo):
    commit(repo, {"img.bin": b"\x00\x01"})
    commit(repo, {"img.bin": b"\x00\x02\x03"})
    d = read_diff(repo, "HEAD~1", "HEAD")
    assert d.files_changed == ["img.bin"]
    assert d.size == 0


def test_renamed_file_reports_its_new_path(repo):
    commit(repo, {"tests/test_old.py": "a\nb\nc\nd\ne\n"})
    git(repo, "mv", "tests/test_old.py", "tests/test_new.py")
    commit(repo, {"tests/test_new.py": "a\nb\nc\nd\ne\nf\n"})
    d = read_diff(repo, "HEAD~1", "HEAD")
    assert d.files_changed == ["tests/test_new.py"]
    assert d.renamed == {"tests/test_new.py": "tests/test_old.py"}
    assert d.added_lines == {"tests/test_new.py": {6}}
    assert d.size == 1


def test_non_ascii_and_spaced_paths(repo):
    commit(repo, {"pkg/café.py": "1\n", "pkg/my file.py": "1\n"})
    commit(repo, {"pkg/café.py": "1\n2\n", "pkg/my file.py": "1\n2\n"})
    d = read_diff(repo, "HEAD~1", "HEAD")
    assert d.added_lines == {"pkg/café.py": {2}, "pkg/my file.py": {2}}
    assert sorted(d.files_changed) == ["pkg/café.py", "pkg/my file.py"]
