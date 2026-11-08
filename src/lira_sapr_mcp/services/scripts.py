"""The agent's own Python code, run on the COM thread with LIRA's objects.

The script sees the names it is given (LIRA's application, the document, the adapter),
prints with ``print`` and returns a value by assigning ``result``. A time limit stops
the script between its own lines; a COM call or a server function that hangs is not
interrupted, and neither is a script that catches BaseException (the worker's own
timeout then answers the agent).
"""

import io
import json
import sys
import time
import traceback
from typing import Any

MAX_TEXT = 20_000
FILENAME = "<script>"


class ScriptTimeout(BaseException):
    """Not an Exception, so that the script's own ``except Exception`` cannot swallow it."""


def run_agent_script(adapter: Any, code: str, ref: str | None, timeout_s: float) -> dict[str, Any]:
    """Run ``code`` on the COM thread with the adapter's LIRA objects for ``ref``."""
    names = adapter.script_names(ref)
    try:
        return run_script(code, names, timeout_s)
    finally:
        names.clear()  # COM objects: release them here, on the COM thread


def run_script(code: str, names: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """Run ``code``; return ok, result (JSON-safe), printed output, error and seconds."""
    try:
        compiled = compile(code, FILENAME, "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"SyntaxError: {exc.msg} (line {exc.lineno})"}

    out = io.StringIO()

    def script_print(*args: Any, sep: str = " ", end: str = "\n", **_: Any) -> None:
        # Never the real stdout: on stdio it carries the MCP protocol.
        out.write(sep.join(map(str, args)) + end)

    deadline = time.monotonic() + timeout_s

    def trace(frame: Any, event: str, arg: Any) -> Any:
        # Only the script's own lines: never stop inside the server's or pywin32's code.
        if frame.f_code.co_filename != FILENAME:
            return None
        if time.monotonic() > deadline:
            raise ScriptTimeout(f"the script ran longer than {timeout_s:g} s and was stopped")
        return trace

    namespace: dict[str, Any] = {"__name__": "__lira_script__", "print": script_print, **names}
    started = time.monotonic()
    answer: dict[str, Any] = {"ok": True}
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        exec(compiled, namespace)
    except BaseException as exc:  # the script may raise anything, even SystemExit
        sys.settrace(previous)  # first: the time limit is for the script, not for us
        answer = {"ok": False, "error": _script_error(exc)}
        exc.__traceback__ = None
    else:
        sys.settrace(previous)
        if "result" in namespace:
            answer["result"] = _json_safe(namespace["result"])
    finally:
        sys.settrace(previous)
        # The namespace holds COM objects: release them here, on the COM thread.
        namespace.clear()
    answer["output"] = _clip(out.getvalue())
    answer["seconds"] = round(time.monotonic() - started, 3)
    return answer


def _script_error(exc: BaseException) -> str:
    """The exception with the script's own lines only, not the server's."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == FILENAME]
    where = "".join(f"  line {f.lineno}, in {f.name}\n" for f in frames)
    message = "".join(traceback.format_exception_only(exc)).strip()
    return _clip(f"{where}{message}" if where else message)


def _json_safe(value: Any) -> Any:
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        return _clip(repr(value))
    return value if len(text) <= MAX_TEXT else _clip(text)


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n... {len(text) - MAX_TEXT} more characters not shown"
