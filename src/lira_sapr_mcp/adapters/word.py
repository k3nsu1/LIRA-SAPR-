"""PDF from a .docx by the Microsoft Word installed on this computer (decision 0005).

Word runs as its own hidden instance (DispatchEx), so documents the user has open in
Word are not touched, and it is closed after saving. The call runs in a thread of its
own with COM initialised there. A hidden Word that shows a window of its own (a
question, an activation message: Word in "reduced functionality mode" cannot save) or
does not finish in time is stopped - only the instance the server started - and the
caller is told why, so it can write the PDF another way.
"""

import contextlib
import threading
import time
from pathlib import Path

from lira_sapr_mcp.adapters.dialogs import DIALOG_CLASS
from lira_sapr_mcp.errors import LiraComError

PDF_FORMAT = 17  # wdExportFormatPDF
NO_WORD = -2147221005  # CO_E_CLASSSTRING: no Word.Application registered
# Word answers "call rejected" / "retry later" while it is still busy, e.g. laying out a
# document it has just opened; late-bound names then fail to resolve (AttributeError).
BUSY = (-2147418111, -2147417846)
CLOSE_GRACE_S = 10.0  # once the PDF is saved, Word has this long to close
EXIT_WAIT_S = 2.0  # after Quit, its process has this long to end


class WordUnavailable(LiraComError):
    """Word could not make the PDF; the message says why. ``lasting``: Word is missing,
    asks something or hangs - it will not do better on the next report."""

    def __init__(self, message: str, lasting: bool) -> None:
        super().__init__(message)
        self.lasting = lasting


def docx_to_pdf(docx: Path, pdf: Path, timeout_s: float = 120.0) -> Path:
    """Save ``docx`` as ``pdf`` (replacing it) with Word."""
    state: dict = {"error": None, "pid": None, "done": False}

    def run() -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        word = doc = None
        try:
            before = _word_pids()
            word = win32com.client.DispatchEx("Word.Application")
            new = _word_pids() - before
            state["pid"] = next(iter(new)) if len(new) == 1 else None
            word.Visible = False
            word.DisplayAlerts = 0
            # Late-bound Word takes positional arguments only: FileName,
            # ConfirmConversions, ReadOnly, AddToRecentFiles.
            doc = word.Documents.Open(str(docx.resolve()), False, True, False)
            _when_ready(lambda: doc.ExportAsFixedFormat(str(pdf.resolve()), PDF_FORMAT), timeout_s)
            state["done"] = True
        except BaseException as exc:  # reported to the caller's thread
            state["error"] = exc
        finally:
            for close in (
                lambda: doc is not None and doc.Close(False),
                lambda: word is not None and word.Quit(),
            ):
                with contextlib.suppress(Exception):
                    close()
            pythoncom.CoUninitialize()

    thread = threading.Thread(target=run, name="word-pdf", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    while thread.is_alive():
        thread.join(0.5)
        pid = state["pid"]
        if state["done"]:  # the PDF is saved; Word only has to close
            deadline = min(deadline, time.monotonic() + CLOSE_GRACE_S)
            if time.monotonic() > deadline:
                _stop(pid)
                break
            continue
        shown = _dialogs(pid) if pid else []
        if shown or time.monotonic() > deadline:
            _stop(pid)
            thread.join(5)
            why = f"it showed a window: {shown[0]}" if shown else f"no answer in {timeout_s:.0f} s"
            raise WordUnavailable(f"Word did not save the PDF ({why})", lasting=True)
    gone_by = time.monotonic() + EXIT_WAIT_S
    while state["pid"] in _word_pids():  # Word did not quit: it is ours, stop it
        if time.monotonic() > gone_by:
            _stop(state["pid"])
            break
        time.sleep(0.2)
    if state["done"]:
        return pdf
    error = state["error"]
    code = getattr(error, "hresult", None) or (error.args[0] if error and error.args else None)
    if code == NO_WORD:
        raise WordUnavailable("Microsoft Word is not installed", lasting=True) from error
    raise WordUnavailable(f"Word did not save the PDF: {error}", lasting=False) from error


def _when_ready(call, timeout_s: float) -> None:
    """Make a call into Word, retrying while Word says it is busy."""
    import pywintypes

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            call()
            return
        except (pywintypes.com_error, AttributeError) as exc:
            busy = isinstance(exc, AttributeError) or exc.hresult in BUSY
            if not busy or time.monotonic() > deadline:
                raise
            time.sleep(0.25)


def _word_pids() -> set[int]:
    """Process ids of running WINWORD.EXE of this user."""
    import win32api
    import win32con
    import win32process

    found = set()
    for pid in win32process.EnumProcesses():
        try:
            h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception:
            continue
        try:
            name = win32process.GetModuleFileNameEx(h, 0)
        except Exception:
            name = ""
        finally:
            win32api.CloseHandle(h)
        if Path(name).name.lower() == "winword.exe":
            found.add(pid)
    return found


def _dialogs(pid: int) -> list[str]:
    """Texts of the visible dialog windows of process ``pid``."""
    import win32gui
    import win32process

    out: list[str] = []

    def each(hwnd: int, _: object) -> bool:
        if (
            win32gui.IsWindowVisible(hwnd)
            and win32gui.GetClassName(hwnd) == DIALOG_CLASS
            and win32process.GetWindowThreadProcessId(hwnd)[1] == pid
        ):
            texts: list[str] = []
            win32gui.EnumChildWindows(
                hwnd, lambda c, acc: acc.append(win32gui.GetWindowText(c)), texts
            )
            text = max(texts, key=len, default="") or win32gui.GetWindowText(hwnd)
            out.append(" ".join(text.split()))
        return True

    win32gui.EnumWindows(each, None)
    return out


def _stop(pid: int | None) -> None:
    """Stop the hidden Word the server started (never another one)."""
    if pid is None:
        return
    import win32api
    import win32con

    try:
        h = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
        win32api.TerminateProcess(h, 1)
        win32api.CloseHandle(h)
    except Exception:
        pass
