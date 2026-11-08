"""Modal windows that LIRA shows, read and answered through Win32, not COM.

While LIRA waits for the user, calls into it may hang, so this module never uses COM and
may run on any thread. A dialog is a visible window of LIRA's process owned by a LIRA
window that it disabled (a modal window), or an ownerless window of the standard dialog
class (a message box). Modeless panels of the standard class are not dialogs.
"""

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lira_sapr_mcp.errors import LiraInputError

DIALOG_CLASS = "#32770"
BUTTON_CLASS = "Button"
LOOKALIKES = str.maketrans("аекорсух", "aekopcyx")  # lower case Cyrillic -> Latin
TEXT_CLASSES = ("Static", "Edit", "RichEdit20W", "RICHEDIT50W")
WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E
SMTO_ABORTIFHUNG = 0x0002
TEXT_TIMEOUT_MS = 500
MAX_TEXT = 4096
IDOK = 1  # the control ids of a dialog's OK, Cancel, "Yes" and "No" buttons, whatever
IDCANCEL = 2  # their captions
IDYES = 6
IDNO = 7


@dataclass
class Dialog:
    hwnd: int
    title: str
    text: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    kind: str = ""  # the window class: DIALOG_CLASS for message boxes

    def describe(self) -> str:
        text = " ".join(self.text)
        return f"'{self.title}': {text} (buttons: {', '.join(self.buttons) or 'none'})"


class Windows(Protocol):
    def top_level(self, exe_name: str) -> list[int]: ...
    def children(self, hwnd: int) -> list[int]: ...
    def class_name(self, hwnd: int) -> str: ...
    def text(self, hwnd: int) -> str: ...
    def owner(self, hwnd: int) -> int: ...
    def enabled(self, hwnd: int) -> bool: ...
    def click(self, hwnd: int) -> None: ...
    def control_id(self, hwnd: int) -> int: ...
    def option_kind(self, hwnd: int) -> str | None: ...
    def set_check(self, hwnd: int, checked: bool) -> None: ...


class Win32Windows:
    """The real windows of this desktop."""

    def top_level(self, exe_name: str) -> list[int]:
        import win32gui
        import win32process

        found: list[int] = []
        names: dict[int, str] = {}

        def visit(hwnd: int, _: Any) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in names:
                names[pid] = _exe_name(pid)
            if names[pid] == exe_name.lower():
                found.append(hwnd)

        win32gui.EnumWindows(visit, None)
        return found

    def children(self, hwnd: int) -> list[int]:
        import win32gui

        found: list[int] = []

        def visit(child: int, _: Any) -> None:
            if win32gui.IsWindowVisible(child):
                found.append(child)

        # A window without children raises on some Windows versions.
        with contextlib.suppress(Exception):
            win32gui.EnumChildWindows(hwnd, visit, None)
        return found

    def class_name(self, hwnd: int) -> str:
        import win32gui

        return win32gui.GetClassName(hwnd)

    def text(self, hwnd: int) -> str:
        """The window's text, asked of the window itself: GetWindowText cannot read the
        text of edit fields in another process. A hung window answers nothing."""
        import ctypes

        send = _send_message_timeout()
        length = ctypes.c_size_t()
        if not send(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, TEXT_TIMEOUT_MS, length):
            return ""
        buffer = ctypes.create_unicode_buffer(min(length.value, MAX_TEXT) + 1)
        copied = ctypes.c_size_t()
        address = ctypes.addressof(buffer)
        if not send(
            hwnd, WM_GETTEXT, len(buffer), address, SMTO_ABORTIFHUNG, TEXT_TIMEOUT_MS, copied
        ):
            return ""
        return buffer.value

    def owner(self, hwnd: int) -> int:
        import win32con
        import win32gui

        return win32gui.GetWindow(hwnd, win32con.GW_OWNER)

    def enabled(self, hwnd: int) -> bool:
        import win32gui

        return bool(win32gui.IsWindowEnabled(hwnd))

    def control_id(self, hwnd: int) -> int:
        import win32gui

        return win32gui.GetDlgCtrlID(hwnd)

    def click(self, hwnd: int) -> None:
        import win32api
        import win32con

        # Posted, not sent: the dialog's own message loop handles it, nothing waits here.
        win32api.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)

    def option_kind(self, hwnd: int) -> str | None:
        """ "check" for a check box, "radio" for a radio button, else None."""
        import win32con
        import win32gui

        if self.class_name(hwnd) != BUTTON_CLASS:
            return None
        kind = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0x0F  # BS_TYPEMASK
        if kind in (win32con.BS_CHECKBOX, win32con.BS_AUTOCHECKBOX, win32con.BS_3STATE,
                    win32con.BS_AUTO3STATE):  # fmt: skip
            return "check"
        if kind in (win32con.BS_RADIOBUTTON, win32con.BS_AUTORADIOBUTTON):
            return "radio"
        return None

    def set_check(self, hwnd: int, checked: bool) -> None:
        import win32con
        import win32gui

        state = win32con.BST_CHECKED if checked else win32con.BST_UNCHECKED
        # Sent, so the option is set before the dialog's OK is posted.
        win32gui.SendMessageTimeout(
            hwnd, win32con.BM_SETCHECK, state, 0, SMTO_ABORTIFHUNG, TEXT_TIMEOUT_MS
        )


def _send_message_timeout() -> Any:
    """user32's SendMessageTimeoutW with its own signature (not the shared ``windll`` one)."""
    import ctypes
    from ctypes import wintypes

    send = ctypes.WinDLL("user32").SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]  # fmt: skip
    send.restype = wintypes.LPARAM
    return send


def _exe_name(pid: int) -> str:
    import win32api
    import win32con
    import win32process

    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return Path(win32process.GetModuleFileNameEx(handle, 0)).name.lower()
    except Exception:  # processes of other users or already gone
        return ""


def find_dialogs(exe: str, windows: Windows | None = None) -> list[Dialog]:
    """The dialogs LIRA (process of ``exe``) shows now."""
    w = windows or Win32Windows()
    tops = w.top_level(Path(exe).name)
    dialogs = []
    for hwnd in tops:
        owner, kind = w.owner(hwnd), w.class_name(hwnd)
        modal = bool(owner) and not w.enabled(owner)  # the owner may be hidden
        if not modal and not (owner == 0 and kind == DIALOG_CLASS):
            continue
        dialog = Dialog(hwnd, w.text(hwnd).strip(), kind=kind)
        for child in w.children(hwnd):
            kind, text = w.class_name(child), _clean(w.text(child))
            if not text:
                continue
            if kind == BUTTON_CLASS:
                dialog.buttons.append(text)
            elif kind in TEXT_CLASSES and len(dialog.text) < 20:
                dialog.text.append(text)
        dialogs.append(dialog)
    return dialogs


def press_button(
    exe: str, button: str, title: str | None = None, windows: Windows | None = None
) -> Dialog:
    """Click a button (by its text, case and '&' ignored) of LIRA's dialog: the only one
    shown, or the one whose title contains ``title``."""
    w = windows or Win32Windows()
    dialogs = find_dialogs(exe, w)
    if title:
        dialogs = [d for d in dialogs if title.casefold() in d.title.casefold()]
    if not dialogs:
        raise LiraInputError("LIRA shows no dialog" + (f" titled {title!r}" if title else ""))
    if len(dialogs) > 1:
        names = "; ".join(repr(d.title) for d in dialogs)
        raise LiraInputError(f"LIRA shows several dialogs ({names}): give the title")
    dialog = dialogs[0]
    wanted = _key(button)
    for child in w.children(dialog.hwnd):
        if w.class_name(child) == BUTTON_CLASS and _key(w.text(child)) == wanted:
            w.click(child)
            return dialog
    raise LiraInputError(f"Dialog {dialog.title!r} has no button {button!r}: {dialog.buttons}")


def answer_no(dialog: Dialog, windows: Windows | None = None) -> bool:
    """Press the "No" button of a question (found by its control id, so in any language
    of LIRA); False when the dialog has none."""
    return press_id(dialog, IDNO, windows)


def answer_yes(dialog: Dialog, windows: Windows | None = None) -> bool:
    """Press the "Yes" button of a question, as ``answer_no`` does "No"."""
    return press_id(dialog, IDYES, windows)


def confirm_unchecked(dialog: Dialog, windows: Windows | None = None) -> bool:
    """Clear every check box of the dialog and press its OK (control id 1, whatever its
    caption, e.g. "Подтвердить"); False when it has none."""
    w = windows or Win32Windows()
    for child in w.children(dialog.hwnd):
        if w.option_kind(child) == "check":
            w.set_check(child, False)
    return press_id(dialog, IDOK, w)


def set_options(hwnd: int, on: set[str], windows: Windows | None = None) -> bool:
    """Check the check boxes and radio buttons of window ``hwnd`` whose captions are in
    ``on`` ('&' and spacing ignored) and clear all the others. False, with nothing
    changed, when a caption in ``on`` is missing: the window is not the one expected."""
    w = windows or Win32Windows()
    options = [c for c in w.children(hwnd) if w.option_kind(c)]
    captions = {c: _clean(w.text(c)) for c in options}
    if not on <= set(captions.values()):
        return False
    for child, caption in captions.items():
        w.set_check(child, caption in on)
    return True


def press_id(dialog: Dialog, control_id: int, windows: Windows | None = None) -> bool:
    """Press the button of ``dialog`` with ``control_id`` (e.g. IDOK, whatever its
    caption); False when it has none."""
    w = windows or Win32Windows()
    for child in w.children(dialog.hwnd):
        if w.class_name(child) == BUTTON_CLASS and w.control_id(child) == control_id:
            w.click(child)
            return True
    return False


def _clean(text: str) -> str:
    return " ".join(text.replace("&", "").split())


def _key(text: str) -> str:
    """Text for matching: no '&', any case, Cyrillic letters that look Latin as Latin
    (LIRA's warnings have a Cyrillic "ОК" button)."""
    return _clean(text).casefold().translate(LOOKALIKES)
