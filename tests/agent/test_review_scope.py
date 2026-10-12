"""Dependencies, security-sensitive code and files tests can't exercise."""

import subprocess

from confidence_agent.gitutil import Diff, read_diff
from confidence_agent.review_scope import (all_dependency_changes, config_only, dependency_changes,
                                           is_dependency_file, risky_code, unmeasured)
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import compute


def test_requirements_changes():
    base = "requests==2.31.0\n# comment\n-r other.txt\nflask>=2\nold-lib\n"
    head = "requests==2.32.0\nflask>=2  # same\nnew_lib[extra]>=1.0\n"
    assert dependency_changes("requirements.txt", base, head) == [
        "requirements.txt: added new-lib[extra] >=1.0",
        "requirements.txt: requests ==2.31.0 → ==2.32.0",
    ]


def test_pyproject_changes_including_extras_and_poetry():
    base = '[project]\ndependencies = ["pytest>=7"]\n'
    head = ('[project]\ndependencies = ["pytest>=8"]\n'
            '[project.optional-dependencies]\ndev = ["black"]\n'
            '[tool.poetry.dependencies]\npython = "^3.9"\nhttpx = "^0.27"\n')
    assert dependency_changes("pyproject.toml", base, head) == [
        "pyproject.toml ([project] dependencies): pytest >=7 → >=8",
        "pyproject.toml (optional-dependencies.dev): added black",
        "pyproject.toml (poetry): added httpx ^0.27",
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
    assert unmeasured(paths) == [".github/workflows/ci.yml", "Dockerfile", "config/settings.yaml"]


def test_signals_scoring_and_config_only_warning():
    diff = Diff(added_lines={"app.py": {2}}, files_changed=["app.py", "deploy.yaml"],
                head_source={"app.py": RISKY}, dependency_changes=["requirements.txt: added x"])
    s = compute(RunResult("b", tests={"t": "passed"}), RunResult("h", tests={"t": "passed"}), diff, "tests")
    assert s.risky_code == ["app.py:2: eval() runs arbitrary code"]
    assert s.unmeasured_files == ["deploy.yaml"]
    assert s.warnings == []
    points = {d[0]: d[1] for d in score(s).deductions}
    assert (points["Security"], points["Dependencies"]) == (20, 5)

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


# --- review fixes ----------------------------------------------------------------

def test_dependency_file_names():
    for path in ("dev-requirements.txt", "test-requirements.txt", "constraints.txt", "requirements-dev.in",
                 "svc/dev-requirements.txt"):
        assert is_dependency_file(path), path
    for path in ("MANIFEST.in", "notes.txt", "README.txt"):
        assert not is_dependency_file(path), path


def test_risky_requirement_lines_are_counted():
    head = ("-e git+https://x/evil.git#egg=evil\n./local_pkg\n-r other.txt\n"
            "--extra-index-url https://pkgs.example/simple\n--hash=sha256:abc\n")
    assert dependency_changes("requirements.txt", "", head) == [
        "requirements.txt: added --extra-index-url https://pkgs.example/simple",
        "requirements.txt: added -e git+https://x/evil.git#egg=evil",
        "requirements.txt: added -r other.txt",
        "requirements.txt: added ./local_pkg",
    ]


def test_every_pyproject_dependency_table_is_read():
    head = """
[build-system]
requires = ["setuptools", "evil-build"]
[dependency-groups]
test = ["pytest-evil"]
[tool.poetry.dev-dependencies]
black = "^24"
[tool.poetry.group.docs.dependencies]
sphinx = "^7"
"""
    added = {c.split("added ", 1)[1].split()[0] for c in dependency_changes("pyproject.toml", "", head)}
    assert added == {"setuptools", "evil-build", "pytest-evil", "black", "sphinx"}


def test_formatting_order_and_moves_arent_changes():
    assert dependency_changes("requirements.txt", "requests>=2.0\n", "requests >= 2.0\n") == []
    two = 'tomli>=1.1; python_version < "3.11"\nfoo==1; sys_platform == "win32"\nfoo==2\n'
    reordered = 'foo==2\nfoo==1; sys_platform == "win32"\ntomli>=1.1; python_version < "3.11"\n'
    assert dependency_changes("requirements.txt", two, reordered) == []
    moved = all_dependency_changes([("requirements.txt", "requests==2.31\n", ""),
                                    ("pyproject.toml", "", '[project]\ndependencies = ["requests==2.31"]\n')])
    assert moved == []


def test_risky_argument_added_on_its_own_line():
    src = "import subprocess, requests\nsubprocess.run(\n    cmd,\n    shell=True,\n)\nrequests.get(\n    url,\n    verify=False,\n)\n"
    found = risky_code("a.py", src, [4, 8])
    assert found == ["a.py:4: subprocess.run(shell=True) runs a shell command",
                     "a.py:8: requests.get(verify=False) turns off TLS certificate checks"]
    assert risky_code("a.py", src, [3]) == []  # changing another argument isn't new risk


def test_imported_names_are_resolved():
    src = ("from subprocess import run\nfrom pickle import loads\nimport pickle as pk\nimport yaml\n"
           "run(c, shell=True)\nloads(b)\npk.load(f)\nyaml.unsafe_load(t)\n")
    found = [f.split(": ", 1)[1] for f in risky_code("a.py", src, range(5, 9))]
    assert found == ["subprocess.run(shell=True) runs a shell command",
                     "pickle.loads() can run code from untrusted data",
                     "pickle.load() can run code from untrusted data",
                     "yaml.unsafe_load() can run code from untrusted data"]


def test_security_false_positives():
    src = ('import yaml, hashlib\nyaml.load(f, yaml.SafeLoader)\nhashlib.md5(b, usedforsecurity=False)\n'
           'TOKEN_URL = "https://auth.example.com/token"\nPASSWORD_ENV_VAR = "APP_PASSWORD"\n'
           'SECRET_PATH = "/run/secrets/db"\n')
    assert risky_code("a.py", src, range(1, 7)) == []


def test_config_only_warning_is_for_infrastructure_only():
    assert config_only(["deploy.yaml", ".github/workflows/ci.yml", "README.md"])
    assert not config_only(["templates/page.html"])
    assert not config_only(["tests/fixtures/rates.json"])
    assert not config_only(["app.py", "deploy.yaml"])


# --- re-review fixes ----------------------------------------------------------

def test_one_file_or_table_cant_hide_a_change_in_another():
    loosened = all_dependency_changes([
        ("pyproject.toml", '[project]\ndependencies = ["django>=4.2"]\n', '[project]\ndependencies = ["django>=1.0"]\n'),
        ("requirements.txt", "django==4.2.1\n", "django==4.2.1\nbar==1\n"),
    ])
    assert "pyproject.toml ([project] dependencies): django >=4.2 → >=1.0" in loosened
    same_file = dependency_changes(
        "pyproject.toml",
        '[project]\ndependencies = ["requests>=2.31"]\n[dependency-groups]\ndev = ["requests"]\n',
        '[project]\ndependencies = ["requests>=0.1"]\n[dependency-groups]\ndev = ["requests"]\n',
    )
    assert same_file == ["pyproject.toml ([project] dependencies): requests >=2.31 → >=0.1"]


def test_removing_a_pin_isnt_reported_as_a_change_elsewhere():
    assert all_dependency_changes([("requirements.txt", "foo==1.5\n", ""),
                                   ("pyproject.toml", '[project]\ndependencies = ["foo>=1"]\n',
                                    '[project]\ndependencies = ["foo>=1"]\n')]) == []


def test_new_extras_are_reported_and_formatting_isnt():
    assert dependency_changes("requirements.txt", "requests==2.31\n", "requests[socks]==2.31\n") == [
        "requirements.txt: added requests[socks] ==2.31"]
    a = 'foo==1; python_version<"3.8" and sys_platform=="win32"\nbar>=1,<2\n'
    b = "foo==1; sys_platform=='win32' and python_version < '3.8'\nbar<2, >=1\n"
    assert dependency_changes("requirements.txt", a, b) == []
    assert is_dependency_file("dev-requirements.in")


def test_secret_heuristics():
    src = ('AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\nTOKENIZER_NAME = "bert-base-uncased"\n'
           'API_TOKEN = "f3a9c2e17b4d"\nPASSWORD_ENV_VAR = "APP_PASSWORD"\n')
    found = [f.split(": ", 1)[1] for f in risky_code("a.py", src, range(1, 5))]
    assert found == ["hard-coded secret in AWS_ACCESS_KEY", "hard-coded secret in API_TOKEN"]


def test_yaml_next_to_tests_isnt_unmeasured():
    # Test data is loaded by tests. YAML under templates/ is often Helm/Kubernetes deployment config, so it counts.
    assert unmeasured(["tests/fixtures/cases.yaml", "testdata/x.yml", "deploy.yaml"]) == ["deploy.yaml"]
    assert not config_only(["tests/fixtures/cases.yaml"])


def test_marker_values_containing_and_stay_whole():
    a = 'foo==1; sys_platform == "android" and python_version >= "3.8"\n'
    b = "foo==1; python_version>='3.8' and sys_platform=='android'\n"
    assert dependency_changes("requirements.txt", a, b) == []
    added = dependency_changes("requirements.txt", "", a)[0]
    assert 'sys_platform=="android"' in added and 'python_version>="3.8"' in added


def test_deployment_templates_are_unmeasured():
    paths = ["charts/app/templates/deployment.yaml", "deploy/templates/k8s.yml"]
    assert unmeasured(paths) == sorted(paths)
    assert config_only(paths)


def test_more_secret_shapes():
    src = ('password = "supersecretpassword"\nMODEL_TOKEN_NAME = "sk-learn-model"\n'
           'STRIPE_PUBLISHABLE_KEY_ENV = "STRIPE_PK"\nOPENAI_API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n')
    found = [f.split(": ", 1)[1] for f in risky_code("a.py", src, range(1, 5))]
    assert found == ["hard-coded secret in password", "hard-coded secret in OPENAI_API_KEY"]
