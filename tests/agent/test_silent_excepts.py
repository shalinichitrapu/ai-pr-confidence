"""New broad except blocks that drop errors without a trace are flagged."""

import subprocess

from confidence_agent.gitutil import Diff, read_diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.swallow import silent_excepts

SOURCE = '''\
def a():
    try:
        pay()
    except Exception:
        pass

def b():
    try:
        pay()
    except:
        return None

def c():
    try:
        pay()
    except (ValueError, Exception) as exc:
        cleanup()

def logged():
    try:
        pay()
    except Exception:
        log.exception("payment failed")

def reraised():
    try:
        pay()
    except Exception as exc:
        raise PaymentError() from exc

def recorded():
    try:
        pay()
    except BaseException as exc:
        span.record_exception(exc)

def narrow():
    try:
        cache[key]
    except KeyError:
        pass
'''


def test_only_silent_broad_handlers_are_found():
    every_line = range(1, len(SOURCE.splitlines()) + 1)
    assert silent_excepts(SOURCE, every_line) == [4, 10, 16]


def test_only_handlers_on_changed_lines_count():
    assert silent_excepts(SOURCE, [10]) == [10]
    assert silent_excepts(SOURCE, [1, 2, 3, 5]) == []  # the except line itself didn't change


def test_unparsable_source_is_skipped():
    assert silent_excepts("def broken(:\n", [1]) == []


def test_signal_and_score():
    from confidence_agent.signals import compute
    diff = Diff(added_lines={"pkg/pay.py": {4, 5}}, head_source={"pkg/pay.py": SOURCE})
    s = compute(RunResult("b"), RunResult("h"), diff, "tests")
    assert s.hidden_code == {"errors silenced without a trace": ["pkg/pay.py:4: except Exception:"]}
    assert any(d[0] == "Visibility" and d[1] == 10 for d in score(s).deductions)


def test_test_files_are_ignored():
    from confidence_agent.signals import compute
    diff = Diff(added_lines={"tests/test_pay.py": {4}}, head_source={"tests/test_pay.py": SOURCE})
    assert compute(RunResult("b"), RunResult("h"), diff, "tests").hidden_code == {}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def test_read_diff_keeps_the_head_source_of_changed_python_files(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "pay.py").write_text("x = 1\n")
    (tmp_path / "notes.md").write_text("a\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "pay.py").write_text("x = 1\ntry:\n    pay()\nexcept Exception:\n    pass\n")
    (tmp_path / "notes.md").write_text("b\n")
    git(tmp_path, "commit", "-qam", "head")
    diff = read_diff(tmp_path, "HEAD~1", "HEAD")
    assert set(diff.head_source) == {"pay.py"}
    assert silent_excepts(diff.head_source["pay.py"], diff.added_lines["pay.py"]) == [4]


MOVED = "def load():\n    try:\n        return read()\n    except Exception:\n        return None\n"


def test_moved_or_reindented_handler_is_not_new():
    from confidence_agent.signals import compute
    d = Diff(added_lines={"pkg/other.py": {1, 2, 3, 4, 5}}, head_source={"pkg/other.py": MOVED},
             removed_text={"pkg/util.py": MOVED.splitlines()})
    assert compute(RunResult("b"), RunResult("h"), d, "tests").hidden_code == {}
    # Re-indented inside a with block: same lines once stripped.
    wrapped = "def load():\n    with lock:\n        try:\n            return read()\n        except Exception:\n            return None\n"
    d = Diff(added_lines={"pkg/util.py": {2, 3, 4, 5, 6}}, head_source={"pkg/util.py": wrapped},
             removed_text={"pkg/util.py": ["    try:", "        return read()", "    except Exception:", "        return None"]})
    assert compute(RunResult("b"), RunResult("h"), d, "tests").hidden_code == {}


def test_one_deletion_explains_only_one_handler():
    from confidence_agent.signals import compute
    two = MOVED + "\n\n" + MOVED.replace("load", "load2")
    d = Diff(added_lines={"pkg/a.py": set(range(1, 13))}, head_source={"pkg/a.py": two},
             removed_text={"pkg/b.py": MOVED.splitlines()})
    assert len(compute(RunResult("b"), RunResult("h"), d, "tests").hidden_code["errors silenced without a trace"]) == 1


def test_form_feed_doesnt_shift_the_quoted_line():
    from confidence_agent.signals import compute
    src = "import os\n\x0c\ndef f():\n    try:\n        os.remove('x')\n    except Exception:\n        pass\n"
    d = Diff(added_lines={"pkg/ff.py": {6, 7}}, head_source={"pkg/ff.py": src})
    found = compute(RunResult("b"), RunResult("h"), d, "tests").hidden_code["errors silenced without a trace"]
    assert found == ["pkg/ff.py:6: except Exception:"]


def test_inner_reraise_and_unrelated_set_status_dont_count_as_handling():
    src = (
        "def a():\n    try:\n        pay()\n    except Exception:\n"
        "        try:\n            conn.rollback()\n        except Exception:\n            raise\n\n"
        "def b():\n    try:\n        pay()\n    except Exception:\n        resp.set_status(200)\n\n"
        "def c():\n    try:\n        pay()\n    except Exception:\n        span.set_status(Status(StatusCode.ERROR))\n"
    )
    assert silent_excepts(src, range(1, 30)) == [4, 13]
