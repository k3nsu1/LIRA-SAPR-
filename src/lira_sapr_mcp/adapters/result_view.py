"""Showing a result in LIRA's window as the engineer does by hand, for pictures of the RPZ
appendices: the results mode, load cases or load combinations (RSN), one item of LIRA's
list (a load case, an RSN or an envelope MAXIMUM / MINIMUM / MAX ABS), a mosaic.

LIRA's API cannot choose what its window shows; its menu commands can (``StartCmd``, numbers
from ``Visor.resources.dll``), except the item of the list. In the ribbon interface that
list lives in the status bar and is drawn by LIRA; its only real window is the text field
of the list (``RichEdit20A``), and a hidden toolbar combo box (id 33447) holds the same
items and the current one. The server opens the list by a click posted next to that text
field and picks the item with the arrow keys, then checks that LIRA shows that item.
Found on LIRA 2024 R2.3 (tests/live/_work/k2_pictures/FINDINGS.md, 2026-11-11).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lira_sapr_mcp.errors import LiraComError, LiraInputError

INPUT_MODE, RESULTS_MODE = 12775, 12776
BY = {"load_cases": 5360, "rsn": 5361}
FRAGMENT, WHOLE_MODEL = 5115, 5117
ENVELOPES = ("MAXIMUM", "MINIMUM", "MAX ABS")
LIST_COMBO_ID = 33447
APPLY = 33446  # "Подтвердить выбор": the view follows the list only after it
# Mosaic -> LIRA command, and the words of LIRA's own caption on the picture.
MOSAICS: dict[str, tuple[int, str]] = {
    "N": (5135, "Мозаика N (расчетные сечения)"),
    "Qy": (45042, "Мозаика Qy (расчетные сечения)"),
    "Qz": (45043, "Мозаика Qz (расчетные сечения)"),
    "My": (45044, "Мозаика My (расчетные сечения)"),
    "Mz": (45045, "Мозаика Mz (расчетные сечения)"),
    "Mx": (5136, "Мозаика Mx (расчетные сечения)"),
    "disp_X": (5203, "Мозаика перемещений по X(G)"),
    "disp_Y": (5204, "Мозаика перемещений по Y(G)"),
    "disp_Z": (5206, "Мозаика перемещений по Z(G)"),
    "Rz": (5243, "Мозаика напряжений по Rz"),
}
# The steel results (LIR-STK), % of the bearing capacity. Numbers from LIRA's own menu
# "Результаты (металл)" (menu 12003 of Visor.resources.dll) and checked on example 12,
# 2026-11-11: they draw in either results mode, the "Металл" tab is not needed (5592 /
# 5594 / 5596 used before are not these mosaics and change nothing).
# Per element, one value for the whole element: the check of the assigned sections ...
_ASSIGNED, _SELECTED = "проверки назначенных сечений", "проверки подобранных сечений"
STEEL_ELEMENT: dict[str, tuple[int, str]] = {
    "steel_1LS": (3814, f"Мозаика результатов {_ASSIGNED} по 1 предельному состоянию"),
    "steel_2LS": (3816, f"Мозаика результатов {_ASSIGNED} по 2 предельному состоянию"),
    "steel_LB": (3817, f"Мозаика результатов {_ASSIGNED} по местной устойчивости"),
    "steel_max": (5090, f"Мозаика максимальных результатов {_ASSIGNED} по 1ПС, 2ПС, МУ"),
    # ... and the check of the sections LIRA selected.
    "steel_sel_1LS": (3815, f"Мозаика результатов {_SELECTED} по 1 предельному состоянию"),
    "steel_sel_2LS": (3823, f"Мозаика результатов {_SELECTED} по 2 предельному состоянию"),
    "steel_sel_LB": (3818, f"Мозаика результатов {_SELECTED} по местной устойчивости"),
    "steel_sel_max": (5098, f"Мозаика максимальных результатов {_SELECTED} по 1ПС, 2ПС, МУ"),
}
# By the design sections of SP / DBN: one check each. LIRA draws them for the assigned or
# the selected sections as set by its menu items "Показывать результаты проверки" (45198)
# / "...подбора" (45199), and for the element types set by its "Для всех типов элементов"
# (45191) and the like. These items are a choice, not a switch: given twice they stay
# (checked on example 12). The server sets them before the mosaic; they stay so after.
STEEL_SECTION_CHECKS: dict[str, tuple[int, str]] = {
    "normal": (45195, "нормальным напряжениям"),
    "shear": (45196, "касательным напряжениям"),
    "reduced": (45197, "приведенным напряжениям"),
    "stab_Y1": (45205, "устойчивости относительно Y1"),
    "stab_Z1": (45206, "устойчивости относительно Z1"),
    "stab_2planes": (45207, "устойчивости в двух плоскостях"),
    "slender_Y1": (45208, "гибкости относительно Y1"),
    "slender_Z1": (45209, "гибкости относительно Z1"),
    "slender_ratio": (45210, "соотношению гибкостей"),
    "beam_stab": (45203, "общей устойчивости балки"),
    "web": (45200, "устойчивости стенки"),
    "flange": (45201, "устойчивости полки"),
    "deflection": (45202, "относительному прогибу балки"),
    "ribs": (45204, "шагу ребер"),
    "lattice": (45213, "шагу решетки"),
}
SHOW_CHECK, SHOW_SELECTED, ALL_ELEMENT_TYPES = 45198, 45199, 45191
STEEL_SECTION: dict[str, tuple[int, str]] = {}
PREPARE: dict[str, tuple[int, ...]] = {}  # commands given before the mosaic's own
for _key, (_cmd, _words) in STEEL_SECTION_CHECKS.items():
    for _prefix, _show, _which in (
        ("steel_sec_", SHOW_CHECK, _ASSIGNED),
        ("steel_sel_sec_", SHOW_SELECTED, _SELECTED),
    ):
        STEEL_SECTION[_prefix + _key] = (_cmd, f"Мозаика результатов {_which} по {_words}")
        PREPARE[_prefix + _key] = (_show, ALL_ELEMENT_TYPES)
MOSAICS.update(STEEL_ELEMENT)
MOSAICS.update(STEEL_SECTION)
STEEL = frozenset(STEEL_ELEMENT) | frozenset(STEEL_SECTION)
VK_UP, VK_DOWN, VK_RETURN = 0x26, 0x28, 0x0D
WM_KEYDOWN, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x100, 0x200, 0x201, 0x202
CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x146, 0x147, 0x148, 0x149
BUTTON_OFFSET = 8  # px right of the list's text field: its drop-down button


def item_index(items: list[str], wanted: int | str) -> int:
    """The position in LIRA's list of a load case or RSN number ("3. Ветер" for 3) or an
    envelope name. Nothing is guessed: a missing item is an error naming what there is."""
    for i, text in enumerate(items):
        if isinstance(wanted, int) and text.split(".", 1)[0].strip() == str(wanted):
            return i
        if isinstance(wanted, str) and text.strip() == wanted:
            return i
    raise LiraInputError(f"LIRA's list has no {wanted!r}; it has: {', '.join(items)}")


def ready(items: list[str], rsn: bool) -> bool:
    """LIRA's list is that of the results: envelopes, and RSN items in the RSN mode."""
    return "MAXIMUM" in items and (not rsn or any("РСН" in t for t in items))


def mosaic_colours(width: int, height: int, bgra: bytes) -> int:
    """How many distinct strong colours cover a noticeable area of a picture: a mosaic and
    its scale have many (8 to 17 on example 04), the bare scheme with its green and blue
    lines few (1 to 5). Colours are taken in steps of 32 per channel."""
    import numpy as np

    px = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)[:, :, :3].astype(np.int16)
    strong = px[(px.max(2) - px.min(2)) > 60] // 32
    if not len(strong):
        return 0
    _, counts = np.unique(strong, axis=0, return_counts=True)
    return int((counts > 200).sum())


MOSAIC_COLOURS = 8  # fewer: LIRA did not draw the mosaic


def scale_band(width: int, height: int, bgra: bytes) -> float:
    """The widest row of strong colours in the top tenth of a picture, as a share of its
    width: LIRA draws a mosaic's scale there across the window (0.67 to 0.94 on examples 04
    and 12, 2026-11-11), the bare scheme has none (0.03 to 0.09). The colours alone are not
    enough: a bare scheme with coloured node and element numbers had 14 of them (example 12,
    a steel mosaic hidden by giving its command twice)."""
    import numpy as np

    px = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)[:, :, :3].astype(np.int16)
    top = px[: max(1, height // 10)]
    return float(((top.max(2) - top.min(2)) > 60).mean(1).max())


SCALE_BAND = 0.4  # less: no scale, so no mosaic (or the user hid LIRA's scale)


def has_mosaic(width: int, height: int, bgra: bytes) -> bool:
    return (
        mosaic_colours(width, height, bgra) >= MOSAIC_COLOURS
        and scale_band(width, height, bgra) >= SCALE_BAND
    )


def keys_to(count: int, index: int) -> list[int]:
    """Keys that pick item ``index`` of an open list of ``count`` items whatever item is
    current: up past the top (Home does not work in LIRA's list), down, Enter."""
    return [VK_UP] * (count + 2) + [VK_DOWN] * index + [VK_RETURN]


def check_mosaic(mosaic: str) -> int:
    if mosaic not in MOSAICS:
        raise LiraInputError(f"Unknown mosaic {mosaic!r}; use one of {', '.join(MOSAICS)}")
    return MOSAICS[mosaic][0]


@dataclass
class StatusList:
    """LIRA's list of load cases / RSN / envelopes in its main window's status bar."""

    main: int
    settle: Callable[[float], None]

    def _children(self) -> list[int]:
        import win32gui

        found: list[int] = []
        win32gui.EnumChildWindows(self.main, lambda h, _: found.append(h), None)
        return found

    @staticmethod
    def _text(hwnd: int) -> str:
        import ctypes

        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.SendMessageW(hwnd, 0x000D, 512, buf)  # WM_GETTEXT
        return buf.value

    def _combo(self) -> int:
        import win32gui

        for h in self._children():
            if win32gui.GetClassName(h) == "ComboBox" and win32gui.GetDlgCtrlID(h) == LIST_COMBO_ID:
                return h
        raise LiraComError("LIRA's list of load cases is not found: is it in the results mode?")

    def items(self) -> list[str]:
        import ctypes

        import win32gui

        combo = self._combo()
        out = []
        for i in range(win32gui.SendMessage(combo, CB_GETCOUNT, 0, 0)):
            size = win32gui.SendMessage(combo, CB_GETLBTEXTLEN, i, 0)
            buf = ctypes.create_unicode_buffer(size + 2)
            ctypes.windll.user32.SendMessageW(combo, CB_GETLBTEXT, i, buf)
            out.append(buf.value)
        return out

    def current(self) -> str:
        import win32gui

        items = self.items()
        i = win32gui.SendMessage(self._combo(), CB_GETCURSEL, 0, 0)
        return items[i] if 0 <= i < len(items) else ""

    def wait_ready(self, rsn: bool, timeout: float = 60.0) -> None:
        """Wait until LIRA has read the results: its list then has the envelopes, and RSN
        items in the RSN mode. A mosaic asked for before that is not drawn (seen on the
        first picture of example 04: the caption of the envelope, no mosaic)."""
        end = time.monotonic() + timeout
        while True:
            try:
                items = self.items()
            except LiraComError:
                items = []
            if ready(items, rsn):
                self.settle(1.0)
                return
            if rsn and ready(items, False) and time.monotonic() > end - timeout + 10:
                raise LiraInputError(
                    "The model has no RSN (LIRA's list: "
                    + ", ".join(items)
                    + "): ask for load cases (case<N>) or add RSN and run the analysis"
                )
            if time.monotonic() > end:
                raise LiraComError(
                    f"LIRA did not show its results in {timeout:.0f} s: run lira_run_analysis?"
                )
            self.settle(0.5)

    def _field(self, items: list[str]) -> tuple[int, int]:
        """The status bar and the text field of the list in it: the visible RichEdit whose
        text is one of the list's items (a hidden one may say the same)."""
        import win32gui

        children = self._children()
        bars = [h for h in children if win32gui.GetClassName(h).startswith("Afx:RibbonStatusBar")]
        for bar in bars:
            fields = [
                h
                for h in children
                if win32gui.GetClassName(h) == "RichEdit20A"
                and win32gui.GetParent(h) == bar
                and win32gui.IsWindowVisible(h)
                and self._text(h) in items
            ]
            if len(fields) == 1:
                return bar, fields[0]
        raise LiraComError(
            "LIRA's list of load cases is not found in its status bar (another interface "
            "style?): choose the item in LIRA's window"
        )

    def _apply(self) -> None:
        """LIRA redraws the view for the list's item only on its "apply" command (seen on
        RSN of example 04: the list said RSN 2, the view still showed RSN 1)."""
        import win32gui

        win32gui.SendMessage(self.main, 0x0111, APPLY, 0)  # WM_COMMAND
        self.settle(1.5)

    def choose(self, wanted: int | str) -> str:
        """Make LIRA show ``wanted``; returns the item's text. Checked, never assumed."""
        import win32api
        import win32gui
        import win32process

        items = self.items()
        index = item_index(items, wanted)
        if self.current() == items[index]:
            self._apply()
            return items[index]
        bar, field = self._field(items)
        _, top, right, bottom = win32gui.GetWindowRect(field)
        x, y = win32gui.ScreenToClient(bar, (right + BUTTON_OFFSET, (top + bottom) // 2))
        point = win32api.MAKELONG(x, y)
        for msg, flag in ((WM_MOUSEMOVE, 0), (WM_LBUTTONDOWN, 1), (WM_LBUTTONUP, 0)):
            win32gui.PostMessage(bar, msg, flag, point)
            self.settle(0.15)
        self.settle(0.6)
        pid = win32process.GetWindowThreadProcessId(self.main)[1]
        popups: list[int] = []
        win32gui.EnumWindows(
            lambda h, _: (
                popups.append(h)
                if h != self.main
                and win32gui.IsWindowVisible(h)
                and win32process.GetWindowThreadProcessId(h)[1] == pid
                else None
            ),
            None,
        )
        if not popups:
            raise LiraComError("LIRA's list of load cases did not open")
        inner: list[int] = []
        win32gui.EnumChildWindows(popups[0], lambda h, _: inner.append(h), None)
        target = inner[0] if inner else popups[0]
        for key in keys_to(len(items), index):
            try:
                win32gui.PostMessage(target, WM_KEYDOWN, key, 0)
            except Exception:  # the list closed itself on Enter
                break
            self.settle(0.05)
        self.settle(1.0)
        self._apply()
        shown = self.current()
        if shown != items[index] or self._text(field) != items[index]:
            raise LiraComError(f"LIRA shows {shown!r} instead of {items[index]!r}")
        return shown


def wait(seconds: float) -> None:  # for tests without a message loop
    time.sleep(seconds)


def run_commands(start: Callable[[int], Any], settle: Callable[[float], None], *cmds: int) -> None:
    for cmd in cmds:
        start(cmd)
        settle(0.8)
