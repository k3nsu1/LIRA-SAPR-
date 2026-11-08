"""The one thread that talks to LIRA.

COM objects created in a single-threaded apartment may only be called from the thread that
created them, so every LIRA call goes through this worker's queue and runs here, one at a
time. Between jobs the worker pumps Windows messages: LIRA delivers its COM events (for
example "analysis finished") as messages to this thread.
"""

import asyncio
import logging
import queue
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from lira_sapr_mcp.errors import LiraTimeoutError

log = logging.getLogger(__name__)

_STOP = object()


def _com_init() -> None:
    import pythoncom

    pythoncom.CoInitialize()


def _com_uninit() -> None:
    import pythoncom

    pythoncom.CoUninitialize()


def _pump() -> None:
    import pythoncom

    pythoncom.PumpWaitingMessages()


def _drop_frame_locals(exc: BaseException) -> None:
    """Free the locals kept in an exception's traceback before it leaves this thread.

    They often hold COM objects; released later by the garbage collector on a thread
    without COM, they fail with CO_E_NOTINITIALIZED.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        current = current.__cause__ or current.__context__


class ComWorker:
    def __init__(
        self,
        *,
        init: Callable[[], None] = _com_init,
        uninit: Callable[[], None] = _com_uninit,
        pump: Callable[[], None] = _pump,
        idle_wait_s: float = 0.02,
    ) -> None:
        self._init = init
        self._uninit = uninit
        self._pump = pump
        self._idle_wait_s = idle_wait_s
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._busy_with: str | None = None
        self._started_at = 0.0
        # A job still running after its caller gave up, and how the last such job ended.
        # The lock orders the caller giving up against the worker finishing the job.
        self._lock = threading.Lock()
        self._overdue: Future | None = None
        self._overdue_job: str | None = None
        self.last_overdue: dict[str, Any] | None = None

    @property
    def busy_with(self) -> str | None:
        """Name of the job running now, or None when idle."""
        return self._busy_with

    @property
    def overdue(self) -> str | None:
        """Name of a job that ran past its time-out and is still running, if any."""
        return self._overdue_job if self._overdue is not None else None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="lira-com", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._jobs.put(_STOP)
            self._thread.join(timeout)
            self._thread = None

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        self.start()
        fut: Future = Future()
        self._jobs.put((fut, fn, args, kwargs))
        return fut

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float, **kwargs: Any) -> Any:
        """Run ``fn`` on the worker and wait for it from any other thread."""
        self._refuse_while_overdue(fn)
        fut = self.submit(fn, *args, **kwargs)
        try:
            return fut.result(timeout)
        except TimeoutError:
            raise self._timed_out(fut, fn, timeout) from None

    async def run(self, fn: Callable[..., Any], *args: Any, timeout: float, **kwargs: Any) -> Any:
        """Run ``fn`` on the worker and await it from the event loop."""
        self._refuse_while_overdue(fn)
        fut = self.submit(fn, *args, **kwargs)
        try:
            # shield: a time-out must not cancel the job here; _timed_out decides.
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout)
        except TimeoutError:
            raise self._timed_out(fut, fn, timeout) from None
        except asyncio.CancelledError:
            # The client gave up (Esc, disconnect): a queued job must not run unseen later.
            if not fut.cancel():
                self._mark_overdue(fut, getattr(fn, "__name__", repr(fn)))
            raise

    def _refuse_while_overdue(self, fn: Callable[..., Any]) -> None:
        """Answer at once while a timed-out job still holds LIRA: queued calls would only
        time out behind it."""
        if self._overdue is not None and not self._overdue.done():
            busy = round(time.monotonic() - self._started_at)
            raise LiraTimeoutError(
                f"LIRA is still busy with {self._busy_with} ({busy} s, past its time-out), "
                f"so {getattr(fn, '__name__', fn)} was not run. Check lira_status or "
                "lira_dialogs; LIRA may be waiting in a dialog. If it never ends (e.g. the "
                "agent's script does not stop), ask the user to restart the MCP server: that "
                "stops the job, while LIRA keeps running."
            )

    def _timed_out(self, fut: Future, fn: Callable[..., Any], timeout: float) -> LiraTimeoutError:
        name = getattr(fn, "__name__", repr(fn))
        if fut.cancel():  # still queued behind another job: it never runs
            return LiraTimeoutError(
                f"LIRA did not start {name} within {timeout:.0f} s: it is busy with "
                f"{self._busy_with or 'another call'}. The call was not run."
            )
        self._mark_overdue(fut, name)
        return LiraTimeoutError(
            f"LIRA did not finish {name} within {timeout:.0f} s. It is still running and may "
            "still complete and change the model: lira_status shows when it ends and whether "
            "it succeeded; do not repeat it before. A modal dialog in LIRA may be waiting "
            "for the user."
        )

    def _mark_overdue(self, fut: Future, name: str) -> None:
        """Remember a job its caller stopped waiting for; if it has just finished,
        record how it ended at once."""
        with self._lock:
            if fut.done():
                error = None if fut.cancelled() else fut.exception()
                self._record_overdue(name, error)
            else:
                self._overdue, self._overdue_job = fut, name

    def _record_overdue(self, name: str | None, error: BaseException | None) -> None:
        self.last_overdue = {
            "job": name,
            "seconds": round(time.monotonic() - self._started_at, 1),
            "ok": error is None,
            "error": None if error is None else str(error),
        }
        log.warning("Job %s finished after its time-out: %s", name, self.last_overdue)
        self._overdue = self._overdue_job = None

    def _loop(self) -> None:
        self._init()
        try:
            while True:
                try:
                    job = self._jobs.get(timeout=self._idle_wait_s)
                except queue.Empty:
                    self._pump()
                    continue
                if job is _STOP:
                    return
                # --8<-- [start:run_job]
                fut, fn, args, kwargs = job
                if not fut.set_running_or_notify_cancel():
                    continue
                self._busy_with = getattr(fn, "__name__", repr(fn))
                self._started_at = time.monotonic()
                error: BaseException | None = None
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    log.debug("job %s failed", self._busy_with, exc_info=exc)
                    _drop_frame_locals(exc)
                    error = exc
                    fut.set_exception(exc)
                # --8<-- [end:run_job]
                finally:
                    with self._lock:
                        if self._overdue is fut:
                            self._record_overdue(self._overdue_job, error)
                    self._busy_with = None
                self._pump()
        finally:
            self._uninit()
