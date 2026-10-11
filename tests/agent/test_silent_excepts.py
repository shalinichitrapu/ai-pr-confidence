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
