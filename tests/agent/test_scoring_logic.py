"""Scoring business logic: the cases the full-repo review reproduced."""

from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import Signals, compute

T1, T2, T3 = "tests/test_a.py::t1", "tests/test_a.py::t2", "tests/test_a.py::t3"
J1, J2, J3 = "tests.test_a::t1", "tests.test_a::t2", "tests.test_a::t3"


def run(outcomes, **kw):
    ids = {J1: T1, J2: T2, J3: T3}
    return RunResult("x", tests=outcomes, node_ids=[ids[j] for j in outcomes], otel_enabled=True, **kw)


def log(test, template="refund failed", location="pay.py:refund", exc="TimeoutError"):
    return {"level": "ERROR", "logger": "app", "template": template, "message": template,
            "exc_type": exc, "location": location, "test": test}


def test_skipping_or_xfailing_a_passing_test_costs_like_removing_it():
    s = compute(run({J1: "passed", J2: "passed"}), run({J1: "passed", J2: "skipped"}), Diff(), "tests")
    r = score(s)
    assert s.tests_skipped == [J2]
    assert r.value == 80 and r.band == "Medium"
    assert not any(c.startswith("All ") for c in r.passed_checks)
    assert "No test newly fails (1 of 2 pass; 1 skipped)" in r.passed_checks


def test_an_error_new_to_a_test_counts_even_if_another_test_already_had_it():
    base = run({J1: "passed", J2: "passed"}, logs=[log(T2)])  # only the error-path test logged it
    head = run({J1: "passed", J2: "passed"}, logs=[log(T2), log(T1)])  # now the happy path does too
    s = compute(base, head, Diff(), "tests")
    assert s.new_error_logs == {"app pay.py:refund: TimeoutError": 1}
    assert score(s).band == "Medium"


def test_logs_stay_within_their_maximum_including_new_tests():
    base = run({J1: "passed"})
    shared = [log(T1, template=f"e{i}", exc=None) for i in range(2)]
    new = [log(T2, template=f"n{i}", exc=None) for i in range(5)]
    head = run({J1: "passed", J2: "passed"}, logs=shared + new)
    r = score(compute(base, head, Diff(), "tests"))
    assert sum(p for sig, p, _ in r.deductions if sig == "Logs") == 25


def test_a_test_that_newly_fails_isnt_also_scored_as_new_errors():
    base = run({J1: "passed", J2: "passed"})
    head = run({J1: "passed", J2: "failed"}, logs=[log(T2)],
               spans=[{"name": "pay", "duration_ms": 1, "status": "ERROR",
                       "events": [{"name": "exception", "exc_type": "TimeoutError", "exc_message": ""}], "test": T2}])
    r = score(compute(base, head, Diff(), "tests"))
    assert [d[0] for d in r.deductions] == ["Tests"]


def test_a_renamed_test_file_keeps_its_baseline():
    base = run({J1: "passed"})
    head = RunResult("h", tests={"tests.test_z::t1": "passed"}, node_ids=["tests/test_z.py::t1"],
                     logs=[log("tests/test_z.py::t1")], otel_enabled=True)
    s = compute(base, head, Diff(renamed={"tests/test_z.py": "tests/test_a.py"}), "tests")
    assert s.tests_moved == {J1: "tests.test_z::t1"}
    assert s.new_error_logs and not s.new_test_error_logs  # full weight, not "only in new tests"


def test_an_existing_test_that_now_expects_an_exception_isnt_penalized():
    before = "def t1():\n    run()\n"
    after = "import pytest\n\ndef t1():\n    with pytest.raises(ValueError):\n        run()\n"
    s = compute(run({J1: "passed"}), run({J1: "passed"}, logs=[log(T1)]),
                Diff(head_source={"tests/test_a.py": after}, base_source={"tests/test_a.py": before}), "tests")
    assert s.new_error_logs == {} and s.new_test_expected_errors
    assert score(s).value == 100


def test_nothing_ran_isnt_also_scored_as_tests_removed():
    r = score(compute(run({J1: "passed", J2: "passed"}), run({}), Diff(), "tests"))
    assert [k for k in r.keys] == ["no-tests"] and r.value == 50


def test_docs_and_lockfiles_dont_count_toward_size():
    d = Diff(file_lines={"docs/guide.md": 500, "poetry.lock": 3000, "app.py": 10})
    s = compute(run({J1: "passed"}), run({J1: "passed"}), d, "tests")
    assert s.code_diff_lines == 10


def test_comments_in_a_new_unimported_file_dont_count_as_untested_code():
    source = "# notes\n" * 90 + "\n" * 8 + "X = 1\nY = 2\n"
    d = Diff(added_lines={"app/notes.py": set(range(1, 101))}, head_source={"app/notes.py": source})
    s = compute(run({J1: "passed"}), run({J1: "passed"}), d, "tests", "app")
    assert s.changed_code_lines == 2


def test_moved_lines_arent_new_untested_code():
    body = "def charge(order):\n    total = order.amount * order.quantity\n    return round(total, 2)\n"
    d = Diff(added_lines={"app/new.py": {1, 2, 3}}, head_source={"app/new.py": body},
             removed_text={"app/old.py": body.splitlines()})
    s = compute(run({J1: "passed"}), run({J1: "passed"}), d, "tests", "app")
    assert s.changed_code_lines == 0  # every line moved unchanged


def test_metric_edge_cases():
    def point(test, value, name="db.queries", **attrs):
        return {"test": test, "name": name, "kind": "updowncounter", "unit": "", "attributes": attrs, "value": value}
    base = run({J1: "passed"}, metrics=[point(T1, -10)], otel_metrics_enabled=True)
    head = run({J1: "passed"}, metrics=[point(T1, -5), point(T1, 3, name="http.requests", timeout_ms="500")],
               otel_metrics_enabled=True)
    head.metrics[1]["kind"] = "counter"
    s = compute(base, head, Diff(), "tests")
    assert s.metric_regressions == {}  # -10 -> -5 isn't "grew at least 2x"
    assert s.new_error_metrics == {}  # "timeout" is an attribute *name* here, not a failure value


def test_coverage_message_names_the_untested_lines():
    r = score(Signals(tests_total=1, changed_code_lines=200, changed_lines_covered=197))
    assert ("Coverage", 1, "3 of 200 changed code lines don't run under any test") in r.deductions


def test_a_function_moved_into_a_new_unimported_module_isnt_new_untested_code():
    body = ["def archive(path):", "    return subprocess.run(['gzip', '-k', path], check=True).returncode"]
    new = '"""Archiving."""\nimport subprocess\n\n\n' + "\n".join(body) + "\n"
    d = Diff(added_lines={"app/archive.py": set(range(1, 7))}, head_source={"app/archive.py": new},
             removed_text={"app/orders.py": body})
    s = compute(run({J1: "passed"}), run({J1: "passed"}), d, "tests", "app")
    assert s.changed_code_lines == 0
