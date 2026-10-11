"""Adding tests, including tests of error paths, must not lower the score."""

from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import compute

OLD = "tests/test_a.py::test_old"
NEW = "tests/test_a.py::test_new_error_path"


def run(ids, logs=(), spans=()):
    return RunResult("x", tests={i: "passed" for i in ids}, node_ids=list(ids),
                     logs=list(logs), spans=list(spans), otel_enabled=True)


def error_log(test, template="bad input"):
    return {"level": "ERROR", "logger": "app", "template": template, "message": template,
            "exc_type": None, "location": "a.py:f", "test": test}


def span(test, name="op", ms=1.0, exc=None):
    events = [{"name": "exception", "exc_type": exc, "exc_message": ""}] if exc else []
    return {"name": name, "duration_ms": ms, "status": "UNSET", "events": events, "test": test}


def test_errors_in_a_new_test_are_listed_but_not_scored():
    head = run([OLD, NEW], logs=[error_log(NEW)], spans=[span(NEW, exc="TypeError")])
    s = compute(run([OLD]), head, Diff(), "tests")
    assert s.new_error_logs == {} and s.new_span_exceptions == {}
    assert s.new_test_error_logs == {"app a.py:f: bad input": 1}
    assert s.new_test_span_exceptions == {"op: TypeError": 1}
    assert score(s).value == 100


def test_errors_in_an_existing_test_are_still_scored():
    head = run([OLD], logs=[error_log(OLD)], spans=[span(OLD, exc="ValueError")])
    s = compute(run([OLD]), head, Diff(), "tests")
    assert s.new_error_logs == {"app a.py:f: bad input": 1}
    assert s.new_span_exceptions == {"op: ValueError": 1}
    assert score(s).value == 75


def test_records_outside_any_test_are_scored():
    s = compute(run([OLD]), run([OLD], logs=[error_log(None)]), Diff(), "tests")
    assert s.new_error_logs == {"app a.py:f: bad input": 1}


def test_slow_spans_from_new_tests_dont_count_as_latency_regressions():
    base = run([OLD], spans=[span(OLD, ms=1.0)])
    head = run([OLD, NEW], spans=[span(OLD, ms=1.0), span(NEW, ms=50.0), span(NEW, ms=50.0)])
    assert compute(base, head, Diff(), "tests").latency_regressions == {}


def test_test_files_dont_count_toward_diff_size():
    diff = Diff(file_lines={"pkg/a.py": 120, "tests/test_a.py": 900, "pkg/test_b.py": 50},
                lines_added=1070)
    s = compute(run([OLD]), run([OLD]), diff, "tests")
    assert s.diff_lines == 1070
    assert s.code_diff_lines == 120
    assert all(d[0] != "Size" for d in score(s).deductions)
