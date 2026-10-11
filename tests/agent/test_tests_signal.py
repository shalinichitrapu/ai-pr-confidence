"""Pre-existing failures, missing results, moved tests and the Low cap."""

from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import Signals, compute
from confidence_agent.gitutil import Diff


def signals(base_tests, head_tests):
    return compute(RunResult("base", tests=base_tests), RunResult("head", tests=head_tests), Diff(), "tests")


def test_any_newly_failing_test_means_low():
    r = score(Signals(tests_total=50, tests_newly_failing=["a"], tests_failed=["a"]))
    assert r.value == 55
    assert r.band == "Low"


def test_failures_from_before_the_change_are_not_reported_as_passing():
    s = signals({"a": "failed", "b": "passed"}, {"a": "failed", "b": "passed"})
    r = score(s)
    assert r.value == 100  # the change didn't cause it
    assert "No test newly fails (1 of 2 already failed before this change)" in r.passed_checks
    assert not any(c.startswith("All ") and "tests pass" in c for c in r.passed_checks)


def test_no_test_results_is_not_a_clean_score():
    r = score(signals({}, {}))
    assert r.value == 50
    assert r.deductions[0][0] == "Tests"
    assert "No test results" in r.deductions[0][2]


def test_test_moved_to_another_module_is_not_removed():
    s = signals(
        {"tests.test_old::test_add": "passed", "tests.test_old::test_gone": "passed"},
        {"tests.test_new::test_add": "passed"},
    )
    assert s.tests_moved == {"tests.test_old::test_add": "tests.test_new::test_add"}
    assert s.tests_removed == ["tests.test_old::test_gone"]


def test_each_added_test_can_only_claim_one_removed_test():
    s = signals({"a.m1::test_x": "passed", "a.m2::test_x": "passed"}, {"a.m3::test_x": "passed"})
    assert len(s.tests_moved) == 1
    assert len(s.tests_removed) == 1


def test_renamed_test_function_still_counts_as_removed():
    s = signals({"t.m::test_old_name": "passed"}, {"t.m::test_new_name": "passed"})
    assert s.tests_removed == ["t.m::test_old_name"]
    assert s.tests_moved == {}
