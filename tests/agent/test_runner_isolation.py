"""The test run must measure the checkout being scored, not some other copy."""

from pathlib import Path

import pytest

from confidence_agent.runner import run_tests

TEST_ADD = "from mypkg import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def broken_working_copy(tmp_path, monkeypatch) -> Path:
    """The user's own repo, importable the way `pip install -e` makes it."""
    repo = write(tmp_path / "repo", {"src/mypkg/__init__.py": "def add(a, b):\n    return a - b\n"})
    monkeypatch.setenv("PYTHONPATH", str(repo / "src"))
    return repo


def test_src_layout_checkout_wins_over_editable_install(tmp_path, monkeypatch):
    repo = broken_working_copy(tmp_path, monkeypatch)
    checkout = write(tmp_path / "checkout", {
        "src/mypkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_add.py": TEST_ADD,
    })
    result = run_tests(checkout, "head", "src/mypkg", "tests", repo=repo)
    assert list(result.tests.values()) == ["passed"], result.output_tail
    assert "src/mypkg/__init__.py" in result.coverage
    assert result.outside_imports == []


def test_imports_from_the_working_copy_are_reported(tmp_path, monkeypatch):
    repo = broken_working_copy(tmp_path, monkeypatch)
    # The checkout has the tests but nothing importable, so Python falls back to the repo.
    checkout = write(tmp_path / "checkout", {"tests/test_add.py": TEST_ADD})
    result = run_tests(checkout, "head", "src/mypkg", "tests", repo=repo)
    assert result.outside_imports == ["src/mypkg/__init__.py"]


def test_parallel_config_still_measures_everything(tmp_path):
    pytest.importorskip("xdist")
    checkout = write(tmp_path / "checkout", {
        "pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-n 2"\n',
        "mypkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_add.py": TEST_ADD + "\ndef test_add_again():\n    assert add(1, 1) == 2\n",
    })
    result = run_tests(checkout, "head", "mypkg", "tests")
    assert len(result.tests) == 2, result.output_tail
    assert result.coverage["mypkg/__init__.py"]["missing_lines"] == []
    assert len(result.node_ids) == 2
    assert len(result.memory) == 2
