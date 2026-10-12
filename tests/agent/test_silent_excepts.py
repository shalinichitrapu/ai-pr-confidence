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
    assert silent_excepts(SOURCE, [1, 2, 3]) == []  # only the try block changed
    assert silent_excepts(SOURCE, [5]) == [4]  # a body-only change (e.g. raise -> pass) counts


def test_unparsable_source_is_skipped():
    assert silent_excepts("def broken(:\n", [1]) == []


def test_signal_and_score():
    from confidence_agent.signals import compute
    diff = Diff(added_lines={"pkg/pay.py": {4, 5}}, head_source={"pkg/pay.py": SOURCE})
    s = compute(RunResult("b"), RunResult("h"), diff, "tests")
    assert s.hidden_code == {"errors silenced without a trace": ["pkg/pay.py:4: except Exception:"]}
    assert any(d[0] == "Visibility" and d[1] == 20 for d in score(s).deductions)


def test_in_tests_only_silenced_assertion_failures_are_reported():
    from confidence_agent.signals import compute
    diff = Diff(added_lines={"tests/test_pay.py": {4}}, head_source={"tests/test_pay.py": SOURCE})
    found = compute(RunResult("b"), RunResult("h"), diff, "tests").hidden_code
    # `except Exception: pass` in a test also swallows AssertionError.
    assert list(found) == ["test failures silenced"]


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


def compute_diff(**kw):
    from confidence_agent.signals import compute
    return compute(RunResult("b"), RunResult("h"), Diff(**kw), "tests").hidden_code


LOGGED = "def load():\n    try:\n        return read()\n    except Exception:\n        log.exception('bad')\n        return None\n"


def test_moved_or_reindented_handler_is_not_new():
    # Moved to another file.
    assert compute_diff(added_lines={"pkg/other.py": {1, 2, 3, 4, 5}}, head_source={"pkg/other.py": MOVED},
                        base_source={"pkg/util.py": MOVED}, removed_lines={"pkg/util.py": {1, 2, 3, 4, 5}}) == {}
    # Re-indented inside a with block.
    wrapped = "def load():\n    with lock:\n        try:\n            return read()\n        except Exception:\n            return None\n"
    assert compute_diff(added_lines={"pkg/util.py": {2, 3, 4, 5, 6}}, head_source={"pkg/util.py": wrapped},
                        base_source={"pkg/util.py": MOVED}, removed_lines={"pkg/util.py": {2, 3, 4, 5}}) == {}


def test_one_removed_handler_explains_only_one_new_one():
    two = MOVED + "\n\n" + MOVED.replace("load", "load2")
    found = compute_diff(added_lines={"pkg/a.py": set(range(1, 13))}, head_source={"pkg/a.py": two},
                         base_source={"pkg/b.py": MOVED}, removed_lines={"pkg/b.py": {1, 2, 3, 4, 5}})
    assert len(found["errors silenced without a trace"]) == 1


def test_unrelated_deletions_dont_hide_a_new_silent_handler():
    # 1. Re-indented while dropping its log call: it used to leave a trace, now it doesn't.
    wrapped = "def load():\n    with lock:\n        try:\n            return read()\n        except Exception:\n            return None\n"
    assert compute_diff(added_lines={"pkg/util.py": {2, 3, 4, 5, 6}}, head_source={"pkg/util.py": wrapped},
                        base_source={"pkg/util.py": LOGGED}, removed_lines={"pkg/util.py": {2, 3, 4, 5, 6}})
    # 2. A logged handler deleted in one file, a new silent one added in another.
    assert compute_diff(added_lines={"pkg/b.py": {1, 2, 3, 4, 5}}, head_source={"pkg/b.py": MOVED},
                        base_source={"pkg/a.py": LOGGED}, removed_lines={"pkg/a.py": {1, 2, 3, 4, 5, 6}})
    # 3. Unrelated deleted lines that happen to read "except Exception:" and "return None".
    other = "def f():\n    try:\n        go()\n    except Exception:\n        raise\n\ndef g():\n    return None\n"
    assert compute_diff(added_lines={"pkg/b.py": {1, 2, 3, 4, 5}}, head_source={"pkg/b.py": MOVED},
                        base_source={"pkg/a.py": other}, removed_lines={"pkg/a.py": {4, 5, 8}})


def test_read_diff_records_removed_base_lines(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "a.py").write_text("x = 1\nz = 3\nw = 4\n")
    git(tmp_path, "commit", "-qam", "head")
    diff = read_diff(tmp_path, "HEAD~1", "HEAD")
    assert diff.removed_lines == {"a.py": {2}}
    assert diff.base_source["a.py"] == "x = 1\ny = 2\nz = 3\n"
    assert diff.added_lines == {"a.py": {3}}


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
