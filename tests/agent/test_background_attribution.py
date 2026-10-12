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


def test_spans_are_credited_to_the_test_running_when_they_start(spans):
    # The documented limit: work handed to a thread that starts after its test ended
    # counts for the test running then. (Inheriting the parent's test instead would
    # misattribute everything a long-lived worker does; see the worker test below.)
    assert spans["app.parent"] == "tests/test_bg.py::test_a_starts_background_work"
    assert spans["app.child"] == "tests/test_bg.py::test_b_runs_while_it_finishes"


WORKER_APP = '''
import asyncio
import logging

from opentelemetry import trace

tracer = trace.get_tracer("app")
log = logging.getLogger("app")


async def worker(queue):
    # A long-lived consumer, like a test server or queue worker shared by many tests.
    while True:
        item = await queue.get()
        with tracer.start_as_current_span("app.handle"):
            log.error("failed to handle %s", item)
        queue.task_done()
'''

WORKER_TESTS = '''
import asyncio

import pytest
from opentelemetry import trace

from app import worker

tracer = trace.get_tracer("tests")


@pytest.fixture(scope="module")
def service():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # Python 3.9 binds the queue to the current loop
    queue = asyncio.Queue()
    with tracer.start_as_current_span("server.start"):  # the worker is created inside a span
        task = loop.create_task(worker(queue))  # ...during the first test's setup
    yield loop, queue
    task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def test_a_uses_service(service):
    pass


def test_b_triggers_error(service):
    loop, queue = service
    loop.run_until_complete(queue.put("x"))
    loop.run_until_complete(queue.join())
'''


def test_long_lived_worker_credits_errors_to_the_test_that_caused_them(tmp_path):
    (tmp_path / "app.py").write_text(WORKER_APP)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_worker.py").write_text(WORKER_TESTS)
    result = run_tests(tmp_path, "head", "app", "tests", track_memory=False)
    assert set(result.tests.values()) == {"passed"}, result.output_tail
    culprit = "tests/test_worker.py::test_b_triggers_error"
    assert [s["test"] for s in result.spans if s["name"] == "app.handle"] == [culprit]
    assert [r["test"] for r in result.logs if r["message"] == "failed to handle x"] == [culprit]
