"""Dependencies, security-sensitive code and files tests can't exercise."""

import subprocess

from confidence_agent.gitutil import Diff, read_diff
from confidence_agent.review_scope import dependency_changes, is_dependency_file, risky_code, unmeasured
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import compute


def test_requirements_changes():
    base = "requests==2.31.0\n# comment\n-r other.txt\nflask>=2\nold-lib\n"
    head = "requests==2.32.0\nflask>=2  # same\nnew_lib[extra]>=1.0\n"
    assert dependency_changes("requirements.txt", base, head) == [
        "requirements.txt: added new-lib >=1.0",
        "requirements.txt: requests ==2.31.0 → ==2.32.0",
    ]


def test_pyproject_changes_including_extras_and_poetry():
    base = '[project]\ndependencies = ["pytest>=7"]\n'
    head = ('[project]\ndependencies = ["pytest>=8"]\n'
            '[project.optional-dependencies]\ndev = ["black"]\n'
            '[tool.poetry.dependencies]\npython = "^3.9"\nhttpx = "^0.27"\n')
    assert dependency_changes("pyproject.toml", base, head) == [
        "pyproject.toml: added black",
        "pyproject.toml: added httpx ^0.27",
        "pyproject.toml: pytest >=7 → >=8",
    ]


def test_dependency_files():
    assert is_dependency_file("requirements.txt") and is_dependency_file("requirements-dev.txt")
    assert is_dependency_file("svc/requirements.txt") and is_dependency_file("pyproject.toml")
    assert not is_dependency_file("README.txt")


RISKY = '''\
import hashlib, os, pickle, subprocess, tempfile, yaml, requests
result = eval(user_input)
data = pickle.loads(blob)
cfg = yaml.load(text)
safe = yaml.load(text, Loader=yaml.SafeLoader)
os.system("rm -rf " + path)
subprocess.run(cmd, shell=True)
subprocess.run(["ls"])
requests.get(url, verify=False)
digest = hashlib.md5(b"x").hexdigest()
name = tempfile.mktemp()
API_KEY = "sk-live-1234567890abcdef"
password = ""
title = "a long string that is fine"
'''


def test_risky_code_on_changed_lines():
    lines = range(1, len(RISKY.splitlines()) + 1)
    found = [f.split(": ", 1)[1] for f in risky_code("app.py", RISKY, lines)]
    assert found == [
        "eval() runs arbitrary code",
        "pickle.loads() can run code from untrusted data",
        "yaml.load() without SafeLoader can run code",
        "os.system() runs a shell command",
        "subprocess.run(shell=True) runs a shell command",
        "requests.get(verify=False) turns off TLS certificate checks",
        "hashlib.md5() is a weak hash",
        "tempfile.mktemp() is open to file races; use mkstemp",
        "hard-coded secret in API_KEY",
    ]


def test_unchanged_lines_are_not_reported():
    assert risky_code("app.py", RISKY, [1, 5, 8, 13, 14]) == []


def test_unmeasured_files():
    paths = ["app.py", "config/settings.yaml", "Dockerfile", ".github/workflows/ci.yml", "README.md",
             "requirements.txt", "data/rates.json"]
    assert unmeasured(paths) == [".github/workflows/ci.yml", "Dockerfile", "config/settings.yaml",
                                 "data/rates.json"]


def test_signals_scoring_and_config_only_warning():
    diff = Diff(added_lines={"app.py": {2}}, files_changed=["app.py", "deploy.yaml"],
                head_source={"app.py": RISKY}, dependency_changes=["requirements.txt: added x"])
    s = compute(RunResult("b", tests={"t": "passed"}), RunResult("h", tests={"t": "passed"}), diff, "tests")
    assert s.risky_code == ["app.py:2: eval() runs arbitrary code"]
    assert s.unmeasured_files == ["deploy.yaml"]
    assert s.warnings == []
    points = {d[0]: d[1] for d in score(s).deductions}
    assert (points["Security"], points["Dependencies"]) == (10, 5)

    only_config = compute(RunResult("b"), RunResult("h"), Diff(files_changed=["deploy.yaml"]), "tests")
    assert "only touches files the tests can't exercise" in only_config.warnings[0]


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_read_diff_compares_dependencies_between_commits(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\nleftpad==1.0\n")
    git(tmp_path, "commit", "-qam", "head")
    assert read_diff(tmp_path, "HEAD~1", "HEAD").dependency_changes == ["requirements.txt: added leftpad ==1.0"]
