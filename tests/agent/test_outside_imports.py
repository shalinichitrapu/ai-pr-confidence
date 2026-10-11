"""The "imported from your working copy" warning must ignore installed packages."""

import sys
import types

from confidence_agent import pytest_plugin


def fake_module(monkeypatch, name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, name, module)


def setup(tmp_path, monkeypatch):
    repo, checkout = tmp_path / "repo", tmp_path / "checkout"
    for d in (repo / ".venv" / "lib" / "site-packages", repo / "src" / "mypkg", checkout):
        d.mkdir(parents=True)
    monkeypatch.setenv("CONFIDENCE_REPO", str(repo))
    monkeypatch.setenv("CONFIDENCE_WORKDIR", str(checkout))
    return repo


def test_virtualenv_inside_the_repo_is_not_reported(tmp_path, monkeypatch):
    repo = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "prefix", str(repo / ".venv"))
    fake_module(monkeypatch, "_fake_pytest", repo / ".venv" / "lib" / "site-packages" / "pytest.py")
    assert pytest_plugin._outside_imports() == []


def test_any_site_packages_folder_is_not_reported(tmp_path, monkeypatch):
    repo = setup(tmp_path, monkeypatch)
    (repo / ".tox" / "py" / "lib" / "site-packages").mkdir(parents=True)
    fake_module(monkeypatch, "_fake_dep", repo / ".tox" / "py" / "lib" / "site-packages" / "dep.py")
    assert pytest_plugin._outside_imports() == []


def test_project_code_from_the_working_copy_is_still_reported(tmp_path, monkeypatch):
    repo = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "prefix", str(repo / ".venv"))
    fake_module(monkeypatch, "_fake_mypkg", repo / "src" / "mypkg" / "__init__.py")
    assert pytest_plugin._outside_imports() == ["src/mypkg/__init__.py"]
