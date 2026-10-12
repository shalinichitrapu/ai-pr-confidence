"""Static checks: the gaps and false positives the full-repo review reproduced."""

import subprocess
from collections import Counter

from confidence_agent.gitutil import Diff, read_diff
from confidence_agent.review_scope import dependency_changes, risky_code
from confidence_agent.runner import RunResult
from confidence_agent.signals import compute
from confidence_agent.swallow import new_silent_excepts, silent_excepts


def hidden(**kw):
    return compute(RunResult("b"), RunResult("h"), Diff(**kw), "tests").hidden_code


# --- silenced errors -------------------------------------------------------------

def test_suppress_exception_is_silent():
    src = "import contextlib\n\ndef f(items):\n    with contextlib.suppress(Exception):\n        price(items)\n"
    assert silent_excepts(src, [4]) == [4]
    assert silent_excepts("from contextlib import suppress\nwith suppress(KeyError):\n    x()\n", [2]) == []


def test_passing_the_error_on_or_logging_at_a_level_is_handling():
    src = ("def a():\n    try:\n        go()\n    except Exception as e:\n        return {'error': str(e)}\n"
           "def b():\n    try:\n        go()\n    except Exception as e:\n        fut.set_exception(e)\n"
           "def c():\n    try:\n        go()\n    except Exception:\n        logger.log(logging.ERROR, 'x', exc_info=True)\n")
    assert silent_excepts(src, range(1, 20)) == []


def test_narrow_handler_counts_when_the_change_also_dropped_error_handling():
    after = "def price(sku):\n    try:\n        return CATALOG[sku]\n    except KeyError:\n        return 0.0\n"
    # The same new narrow handler: harmless on its own, flagged when the change removed a raise.
    assert hidden(added_lines={"app/p.py": {4, 5}}, head_source={"app/p.py": after}) == {}
    found = hidden(added_lines={"app/p.py": {4, 5}}, head_source={"app/p.py": after},
                   removed_text={"app/p.py": ["        raise UnknownSku(sku)"]}, removed_lines={"app/p.py": {5}})
    assert "errors silenced without a trace" in found


def test_silencing_assertions_in_tests_is_flagged():
    src = "def check(x):\n    try:\n        assert x == 1\n    except BaseException:\n        pass\n"
    found = hidden(added_lines={"tests/helpers.py": {4, 5}}, head_source={"tests/helpers.py": src})
    assert list(found) == ["test failures silenced"]


def test_reformatting_a_silent_handler_isnt_new():
    before = "def f():\n    try:\n        go()\n    except Exception:\n        pass\n"
    after = "def f():\n    try:\n        go()\n    except Exception:  # best effort\n        pass\n"
    removed = Counter()
    from confidence_agent.swallow import removed_silent_handlers
    removed = removed_silent_handlers({"a.py": before}, {"a.py": {4}})
    assert new_silent_excepts("a.py", after, [4], removed) == []


# --- removed instrumentation -------------------------------------------------------

DEAD = "def legacy(order):\n    logger.error('old path')\n    order.set_status('paid')\n    return None\n"


def test_deleting_a_dead_function_isnt_hiding():
    found = hidden(added_lines={"app/m.py": set()}, removed_text={"app/m.py": DEAD.splitlines()},
                   removed_lines={"app/m.py": {1, 2, 3, 4}}, base_source={"app/m.py": DEAD})
    assert found == {}


def test_logger_name_variants_and_domain_set_status():
    removed = ["    _LOGGER.error('x')", "    logging.getLogger(__name__).exception('y')", "    order.set_status('paid')"]
    found = hidden(added_lines={"app/m.py": {9}}, removed_text={"app/m.py": removed},
                   removed_lines={"app/m.py": {2, 3, 4}}, base_source={"app/m.py": "x = 1\n" * 9})
    assert list(found) == ["error logging removed"] and len(found["error logging removed"]) == 2


def test_weakened_assertions_are_flagged():
    found = hidden(added_lines={"tests/test_a.py": {3}}, added_text={"tests/test_a.py": ["    assert total is not None"]},
                   removed_text={"tests/test_a.py": ["    assert total == 107.5"]})
    assert found == {"test assertions weakened": ["tests/test_a.py: assert total == 107.5"]}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_test_collection_tricks_and_pytest_config_changes(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (tmp_path / "conftest.py").write_text("")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-k not slow"\n')
    (tmp_path / "conftest.py").write_text('collect_ignore = ["tests/test_legacy.py"]\n')
    git(tmp_path, "commit", "-qam", "head")
    d = read_diff(tmp_path, "HEAD~1", "HEAD")
    assert d.pytest_config_changes == ["pyproject.toml: addopts"]
    found = compute(RunResult("b"), RunResult("h"), d, "tests").hidden_code
    assert found["test collection or configuration changed"] == [
        'conftest.py: collect_ignore = ["tests/test_legacy.py"]', "pytest settings changed in pyproject.toml: addopts"]


# --- dependencies and security --------------------------------------------------------

def test_setup_py_and_setup_cfg_dependencies():
    head_py = "from setuptools import setup\nsetup(name='x', install_requires=['requests>=2', 'leftpad==1.0'])\n"
    assert dependency_changes("setup.py", "from setuptools import setup\nsetup(install_requires=['requests>=2'])\n",
                              head_py) == ["setup.py (install_requires): added leftpad ==1.0"]
    head_cfg = "[options]\ninstall_requires =\n    requests>=2\n    leftpad==1.0\n"
    assert dependency_changes("setup.cfg", "[options]\ninstall_requires =\n    requests>=2\n", head_cfg) == [
        "setup.cfg (install_requires): added leftpad ==1.0"]


def test_obfuscated_calls_and_shell_variables():
    src = ("import os, subprocess\ngetattr(os, 'system')(cmd)\n__import__('pickle').loads(blob)\n"
           "subprocess.Popen(cmd, shell=use_shell)\n")
    found = [f.split(": ", 1)[1] for f in risky_code("a.py", src, [2, 3, 4])]
    assert found == ["os.system() runs a shell command", "pickle.loads() can run code from untrusted data",
                     "subprocess.Popen(shell=use_shell) may run a shell command"]


def test_secret_detection_names_and_places():
    src = ('TOKENIZER = "cl100k_base"\nTOKEN_HEADER = "Authorization"\nTOKEN_URL = "v1/oauth/token"\n'
           'PASSWORD_RESET_TEMPLATE = "emails/reset_v2.html"\n'
           'connect(password="s3cr3tpassw0rd")\nCONFIG = {"api_key": "AKIAIOSFODNN7EXAMPLE"}\n')
    found = [f.split(": ", 1)[1] for f in risky_code("a.py", src, range(1, 7))]
    assert found == ["hard-coded secret in password", "hard-coded secret in api_key"]


def test_ignore_setting_applies_to_pyproject_dependency_entries():
    from confidence_agent.config import parse
    d = Diff(files_changed=["pyproject.toml"], dependency_changes=["pyproject.toml (poetry): added x ^1"])
    s = compute(RunResult("b"), RunResult("h"), d, "tests", ".", parse('[tool.confidence]\nignore = ["pyproject.toml"]\n'))
    assert s.dependency_changes == []


def test_mentioning_the_error_in_a_debug_log_isnt_handling_it():
    src = ("def total(lines):\n    try:\n        return price(lines)\n    except Exception as exc:\n"
           "        logger.debug('could not price order: %s', exc)\n        return 0.0\n")
    assert silent_excepts(src, [4, 5, 6]) == [4]
