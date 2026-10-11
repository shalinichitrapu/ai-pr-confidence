"""One new swallowed error is enough to leave High, even without tracing."""

from confidence_agent.gitutil import Diff
from confidence_agent.runner import RunResult
from confidence_agent.scoring import score
from confidence_agent.signals import compute

T = "tests/test_pay.py::test_charge"


def run(logs=()):
    return RunResult("x", tests={"tests.test_pay::test_charge": "passed"}, node_ids=[T], logs=list(logs))


def test_single_swallowed_error_without_tracing_is_medium():
    swallowed = {"level": "ERROR", "logger": "app", "template": "payment failed", "message": "payment failed",
                 "exc_type": "ValueError", "location": "pay.py:charge", "test": T}
    r = score(compute(run(), run([swallowed]), Diff(), "tests"))
    assert r.value == 80
    assert r.band == "Medium"
