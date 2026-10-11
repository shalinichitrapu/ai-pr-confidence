"""Spans from background work are credited to the test that started them."""

from pathlib import Path

import pytest

from confidence_agent.runner import run_tests

APP = '''
import threading
import time

from opentelemetry import context, trace

tracer = trace.get_tracer("app")
threads = []


def slow_job():
    def job():
        with tracer.start_as_current_span("app.slow_job"):
            time.sleep(0.4)  # still running after the test that started it returns
    t = threading.Thread(target=job)
    t.start()
    threads.append(t)


def handed_off():
    # A span whose child runs in a thread that was given the trace context.
    with tracer.start_as_current_span("app.parent"):
        ctx = context.get_current()

        def child():
            token = context.attach(ctx)
            try:
                time.sleep(0.4)
                with tracer.start_as_current_span("app.child"):
                    pass
            finally:
                context.detach(token)
        t = threading.Thread(target=child)
        t.start()
        threads.append(t)
'''

TESTS = '''
import time

from app import handed_off, slow_job, threads


def test_a_starts_background_work():
    slow_job()
    handed_off()


def test_b_runs_while_it_finishes():
    time.sleep(0.8)
    for t in threads:
        t.join()
'''


@pytest.fixture(scope="module")
def spans(tmp_path_factory):
    root = tmp_path_factory.mktemp("bg")
    (root / "app.py").write_text(APP)
    (root / "tests").mkdir()
    (root / "tests" / "test_bg.py").write_text(TESTS)
    result = run_tests(root, "head", "app", "tests", track_memory=False)
    assert set(result.tests.values()) == {"passed"}, result.output_tail
    return {s["name"]: s["test"] for s in result.spans}


def test_span_finishing_during_a_later_test_belongs_to_the_test_that_started_it(spans):
    assert spans["app.slow_job"] == "tests/test_bg.py::test_a_starts_background_work"


def test_child_span_started_later_in_a_thread_inherits_its_parents_test(spans):
    assert spans["app.parent"] == "tests/test_bg.py::test_a_starts_background_work"
    assert spans["app.child"] == "tests/test_bg.py::test_a_starts_background_work"
