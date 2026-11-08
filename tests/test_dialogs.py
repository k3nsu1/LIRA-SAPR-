"""LIRA's dialogs: found among its windows, answered by button, reported on time-outs."""

import time

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.adapters.dialogs import (
    Dialog,
    Win32Windows,
    answer_no,
    answer_yes,
    confirm_unchecked,
    find_dialogs,
    press_button,
    set_options,
)
from lira_sapr_mcp.errors import LiraInputError, LiraTimeoutError
from lira_sapr_mcp.server import build_server

MAIN, ABOUT, PALETTE, PANEL, BOX = 1, 2, 3, 4, 5


class FakeWindows:
    """LIRA's main window (disabled by a modal About box) and a tool palette; with the
    main window enabled, a modeless panel of the dialog class and a message box."""

    def __init__(self, main_enabled=False):
        self.windows = {
            MAIN: ("Afx:main", "ЛИРА-САПР 2024", 0, main_enabled, []),
            ABOUT: ("#32770", "О программе", MAIN, True, [10, 11, 12, 13]),
            PALETTE: ("Afx:palette", "Палитра", MAIN, True, [20]),
        }
        if main_enabled:
            del self.windows[ABOUT]
            self.windows[PANEL] = ("#32770", "Информация об узле", MAIN, True, [20])
            self.windows[BOX] = ("#32770", "Внимание", 0, True, [12])
        self.controls = {
            10: ("Static", "ЛИРА-САПР 2024 R2.3"),
            11: ("Static", ""),
            12: ("Button", "&OK"),
            13: ("Button", "Отмена"),
            20: ("Button", "Кнопка палитры"),
        }
        self.clicked = []
        self.options = {}  # control -> "check" or "radio"
        self.checked = {}  # option -> whether it is checked

    def top_level(self, exe_name):
        assert exe_name == "LiraSapr.exe"
        return list(self.windows)

    def children(self, hwnd):
        return self.windows[hwnd][4]

    def class_name(self, hwnd):
        return self.windows[hwnd][0] if hwnd in self.windows else self.controls[hwnd][0]

    def text(self, hwnd):
        return self.windows[hwnd][1] if hwnd in self.windows else self.controls[hwnd][1]

    def owner(self, hwnd):
        return self.windows[hwnd][2]

    def enabled(self, hwnd):
        return self.windows[hwnd][3]

    def click(self, hwnd):
        self.clicked.append(hwnd)

    def control_id(self, hwnd):
        return {12: 1, 13: 2, 14: 6, 15: 7}.get(hwnd, 0)

    def option_kind(self, hwnd):
        return self.options.get(hwnd)

    def set_check(self, hwnd, checked):
        self.checked[hwnd] = checked


EXE = r"C:\LIRA\LiraSapr.exe"


def test_dialogs_are_dialog_windows_and_windows_that_block_lira():
    found = find_dialogs(EXE, FakeWindows())
    # The palette is owned by the disabled main window, so it blocks LIRA too.
    assert [d.title for d in found] == ["О программе", "Палитра"]
    about = found[0]
    assert about.text == ["ЛИРА-САПР 2024 R2.3"] and about.buttons == ["OK", "Отмена"]
    assert "buttons: OK, Отмена" in about.describe()


def test_windows_that_do_not_block_lira_are_not_dialogs_except_a_message_box():
    # The palette and the panel of the dialog class are modeless: LIRA's main window
    # stays enabled. An ownerless window of the dialog class is a message box.
    assert [d.title for d in find_dialogs(EXE, FakeWindows(main_enabled=True))] == ["Внимание"]


def test_a_question_owned_by_a_hidden_disabled_window_is_a_dialog():
    """As LIRA's question during the text export of a model with construction stages:
    its owner is a hidden window of LIRA, disabled while the question is open."""
    w = FakeWindows(main_enabled=True)
    hidden = 9
    w.windows[hidden] = ("Afx:hidden", "", 0, False, [])
    w.windows[6] = ("#32770", "Предупреждение", hidden, True, [12])
    w.top_level = lambda exe_name: [h for h in w.windows if h != hidden]  # not visible
    assert "Предупреждение" in [d.title for d in find_dialogs(EXE, w)]


def test_a_question_is_answered_no_by_the_button_id_in_any_language():
    w = FakeWindows(main_enabled=True)
    w.controls |= {14: ("Button", "&Yes"), 15: ("Button", "&No")}
    w.windows[6] = ("#32770", "Warning", MAIN, True, [14, 15])
    assert answer_no(Dialog(6, "Warning"), w) and w.clicked == [15]
    assert answer_yes(Dialog(6, "Warning"), w) and w.clicked == [15, 14]
    assert not answer_no(Dialog(PANEL, "Информация об узле"), w)  # no "No" button
    assert not answer_yes(Dialog(PANEL, "Информация об узле"), w)


def test_a_question_is_confirmed_with_every_box_cleared():
    """As LIRA's question about a soil model: confirm, recalculating nothing."""
    w = FakeWindows(main_enabled=True)
    w.controls |= {16: ("Button", "пересчитать C1 и C2"), 17: ("Button", "пересчитать сваи")}
    w.options, w.checked = {16: "check", 17: "check"}, {16: True, 17: False}
    w.windows[6] = ("#32770", "Предупреждение", MAIN, True, [16, 17, 12, 13])
    assert confirm_unchecked(Dialog(6, "Предупреждение"), w)
    assert w.checked == {16: False, 17: False} and w.clicked == [12]
    assert not confirm_unchecked(Dialog(PANEL, "Информация об узле"), w)  # no OK


def test_options_are_set_by_caption_and_only_in_the_window_expected():
    """As LIRA's packing panel: one box and one radio button on, all others off."""
    w = FakeWindows(main_enabled=True)
    w.controls |= {
        16: ("Button", "Выполнить &сшивку"),
        17: ("Button", "Удаленные узлы и элементы"),
        18: ("Button", "Не удалять элементы"),
        19: ("Button", "По возможности исправлять"),
    }
    w.options = {16: "check", 17: "check", 18: "radio", 19: "radio"}
    w.checked = {16: True, 17: False, 18: False, 19: True}
    w.windows[6] = ("#32770", "Упаковка", MAIN, True, [16, 17, 18, 19, 12])
    assert not set_options(6, {"Удаленные узлы и элементы", "Нет такого"}, w)
    assert w.checked == {16: True, 17: False, 18: False, 19: True}  # untouched
    assert set_options(6, {"Удаленные узлы и элементы", "Не удалять элементы"}, w)
    assert w.checked == {16: False, 17: True, 18: True, 19: False}


def test_press_finds_the_button_by_text():
    w = FakeWindows(main_enabled=True)
    pressed = press_button(EXE, "ok", windows=w)
    assert pressed.title == "Внимание" and w.clicked == [12]


def test_press_takes_latin_ok_for_a_cyrillic_button():
    w = FakeWindows(main_enabled=True)
    w.controls[12] = ("Button", "ОК")  # Cyrillic letters, as in LIRA's warnings
    assert press_button(EXE, "OK", windows=w).title == "Внимание" and w.clicked == [12]


def test_press_needs_one_dialog_and_a_real_button():
    with pytest.raises(LiraInputError, match="several dialogs"):
        press_button(EXE, "OK", windows=FakeWindows())
    with pytest.raises(LiraInputError, match="no button 'Да'"):
        press_button(EXE, "Да", title="программе", windows=FakeWindows())
    with pytest.raises(LiraInputError, match="no dialog titled 'Ошибка'"):
        press_button(EXE, "OK", title="Ошибка", windows=FakeWindows())


@pytest.mark.anyio
async def test_a_time_out_names_the_dialog_lira_waits_in(tmp_path):
    session, adapter = fake_session(tmp_path)
    adapter.dialogs_shown.append(Dialog(5, "Внимание", ["Сохранить изменения?"], ["Да", "Нет"]))
    with pytest.raises(LiraTimeoutError, match="'Внимание': Сохранить изменения"):
        await session.run(time.sleep, 1.0, timeout=0.2)
    adapter.dialogs_shown.clear()
    with pytest.raises(LiraTimeoutError) as plain:
        await session.run(time.sleep, 1.0, timeout=0.2)
    assert "shows a dialog" not in str(plain.value)
    session.close()


@pytest.mark.anyio
async def test_a_time_out_tells_dialogs_of_the_call_from_older_ones(tmp_path):
    session, adapter = fake_session(tmp_path)
    users = Dialog(5, "Параметры", ["Шаг сетки"], ["OK"])
    adapter.dialogs_shown.append(users)

    def stuck():
        adapter.dialogs_shown.append(Dialog(6, "Ошибка", ["Нет файла"], ["OK"]))
        time.sleep(1.0)

    with pytest.raises(LiraTimeoutError) as timed_out:
        await session.run(stuck, timeout=0.2)
    message = str(timed_out.value)
    assert "opened during this call: 'Ошибка'" in message
    assert "open before this call, perhaps the user's: 'Параметры'" in message
    session.close()


def test_text_of_an_edit_field_in_another_process_is_read(tmp_path):
    """GetWindowText returns nothing for another process's edit field; WM_GETTEXT does."""
    import subprocess
    import sys
    import textwrap

    import win32gui

    ready = tmp_path / "hwnd.txt"
    code = textwrap.dedent("""
        import pathlib, sys, time, win32con, win32gui
        top = win32gui.CreateWindow("STATIC", "lira-mcp-test", win32con.WS_OVERLAPPED,
                                    0, 0, 200, 100, 0, 0, 0, None)
        edit = win32gui.CreateWindow("EDIT", "", win32con.WS_CHILD,
                                     0, 0, 100, 20, top, 0, 0, None)
        # As if typed: the field's own text, not the window caption that WM_SETTEXT sets.
        win32gui.SendMessage(edit, win32con.EM_REPLACESEL, 0, "текст поля")
        flag = pathlib.Path(sys.argv[1])
        flag.write_text(str(edit))
        end = time.time() + 20
        while time.time() < end and flag.exists():
            win32gui.PumpWaitingMessages()
            time.sleep(0.01)
    """)
    child = subprocess.Popen([sys.executable, "-c", code, str(ready)])
    try:
        for _ in range(500):
            if ready.exists() and ready.read_text():
                break
            time.sleep(0.02)
        edit = int(ready.read_text())
        assert win32gui.GetWindowText(edit) == ""
        assert Win32Windows().text(edit) == "текст поля"
    finally:
        ready.unlink(missing_ok=True)
        child.wait(10)


@pytest.mark.anyio
async def test_dialog_tools_list_and_press(tmp_path):
    session, adapter = fake_session(tmp_path)
    adapter.dialogs_shown.append(Dialog(5, "Внимание", ["Сохранить?"], ["Да", "Нет"]))
    async with Client(build_server(session)) as client:
        listed = await client.call_tool("lira_dialogs", {})
        pressed = await client.call_tool("lira_press_dialog_button", {"button": "Нет"})
        none = await client.call_tool("lira_press_dialog_button", {"button": "Нет"})
    assert listed.structured_content["dialogs"][0]["buttons"] == ["Да", "Нет"]
    assert pressed.structured_content["pressed"] == {"title": "Внимание", "button": "Нет"}
    assert pressed.structured_content["dialogs"] == []
    assert none.is_error
