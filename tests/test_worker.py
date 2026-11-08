import asyncio
import threading
import time

import pytest

from lira_sapr_mcp.com.worker import ComWorker
from lira_sapr_mcp.errors import LiraTimeoutError


@pytest.fixture
def worker():
    w = ComWorker(init=lambda: None, uninit=lambda: None, pump=lambda: None)
    yield w
    w.stop()


def test_jobs_run_on_one_dedicated_thread(worker):
    caller = threading.get_ident()
    ids = {worker.call(threading.get_ident, timeout=5) for _ in range(5)}
    assert len(ids) == 1
    assert caller not in ids


def test_exceptions_reach_the_caller(worker):
    def boom():
        raise ValueError("bad table")

    with pytest.raises(ValueError, match="bad table"):
        worker.call(boom, timeout=5)


def test_timeout_names_the_running_job_and_skips_cancelled_ones(worker):
    release = threading.Event()
    ran = []

    def hanging_dialog():
        release.wait(5)

    worker.submit(hanging_dialog)
    time.sleep(0.05)
    with pytest.raises(LiraTimeoutError, match="hanging_dialog"):
        worker.call(lambda: ran.append(1), timeout=0.1)
    release.set()
    assert worker.call(lambda: "after", timeout=5) == "after"
    assert ran == []


def test_async_run_returns_result(worker):
    async def main():
        return await worker.run(lambda a, b: a + b, 2, 3, timeout=5)

    assert asyncio.run(main()) == 5


def test_async_run_times_out(worker):
    release = threading.Event()
    worker.submit(lambda: release.wait(5))

    async def main():
        await worker.run(lambda: None, timeout=0.1)

    with pytest.raises(LiraTimeoutError):
        asyncio.run(main())
    release.set()


def test_pump_runs_while_idle():
    pumped = threading.Event()
    w = ComWorker(init=lambda: None, uninit=lambda: None, pump=pumped.set)
    w.start()
    try:
        assert pumped.wait(2)
    finally:
        w.stop()


def test_a_queued_call_that_times_out_is_never_run(worker):
    release = threading.Event()
    ran = []
    worker.submit(lambda: release.wait(5))
    time.sleep(0.05)

    async def main():
        await worker.run(lambda: ran.append(1), timeout=0.1)

    with pytest.raises(LiraTimeoutError, match="was not run"):
        asyncio.run(main())
    release.set()
    worker.call(lambda: None, timeout=5)
    assert ran == []


def test_an_overdue_job_blocks_new_calls_and_reports_how_it_ended(worker):
    release = threading.Event()

    def set_loads():
        release.wait(5)
        return "saved"

    async def first():
        await worker.run(set_loads, timeout=0.1)

    with pytest.raises(LiraTimeoutError, match="still running and may still complete"):
        asyncio.run(first())
    assert worker.overdue == "set_loads"
    # Other calls are refused at once instead of queueing behind it.
    started = time.monotonic()
    with pytest.raises(LiraTimeoutError, match="still busy with set_loads") as refused:
        worker.call(lambda: None, timeout=5)
    assert "restart the MCP server" in str(refused.value)
    assert time.monotonic() - started < 1
    release.set()
    for _ in range(100):
        if worker.overdue is None:
            break
        time.sleep(0.02)
    assert worker.last_overdue["job"] == "set_loads" and worker.last_overdue["ok"]
    assert worker.call(lambda: "free", timeout=5) == "free"


def test_an_overdue_job_that_fails_reports_its_error(worker):
    release = threading.Event()

    def save():
        release.wait(5)
        raise RuntimeError("disk full")

    with pytest.raises(LiraTimeoutError):
        worker.call(save, timeout=0.1)
    release.set()
    for _ in range(100):
        if worker.last_overdue:
            break
        time.sleep(0.02)
    assert worker.last_overdue["ok"] is False and "disk full" in worker.last_overdue["error"]


def test_a_cancelled_call_still_queued_is_never_run(worker):
    release = threading.Event()
    ran = []
    worker.submit(lambda: release.wait(5))
    time.sleep(0.05)

    async def main():
        task = asyncio.create_task(worker.run(lambda: ran.append(1), timeout=5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    release.set()
    worker.call(lambda: None, timeout=5)
    assert ran == [] and worker.overdue is None


def test_a_cancelled_call_already_running_counts_as_overdue(worker):
    release = threading.Event()

    def set_loads():
        release.wait(5)

    async def main():
        task = asyncio.create_task(worker.run(set_loads, timeout=5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert worker.overdue == "set_loads"
    release.set()
    for _ in range(100):
        if worker.last_overdue:
            break
        time.sleep(0.02)
    assert worker.last_overdue["job"] == "set_loads" and worker.overdue is None


def test_a_job_that_ends_as_its_caller_gives_up_is_recorded_not_left_overdue(worker):
    from concurrent.futures import Future

    fut = Future()
    fut.set_running_or_notify_cancel()
    fut.set_result("saved")  # finished just before the caller's time-out was handled

    def save():
        pass

    error = worker._timed_out(fut, save, 0.1)
    assert "still running" in str(error)
    assert worker.overdue is None
    assert worker.last_overdue["job"] == "save" and worker.last_overdue["ok"]
