"""ComLiraAdapter logic that does not need LIRA, on stand-ins for its COM objects."""

import threading
import time

import pytest

from lira_sapr_mcp.adapters.com_adapter import ComLiraAdapter
from lira_sapr_mcp.adapters.discovery import LiraInstall
from lira_sapr_mcp.errors import LiraComError, LiraInputError


class Doc:
    def __init__(self, path):
        self.PathName, self.Title, self.SystemLabel = path, "m", 5
        self.modified = False
        self.saves = 0
        self.closed = False

    def IsModified(self):
        return self.modified

    def Save(self, _ask):
        self.saves += 1
        self.modified = False

    def Close(self):
        self.closed = True

    def Activate(self):
        pass


class App:
    def __init__(self, doc):
        self.doc = doc
        self.ActiveDocument = doc
        self.DocumentCount = 1
        self.fail = False

    def Document(self, _i):
        return self.doc

    def StartAnalysis(self, _path):
        if self.fail:
            raise OSError("COM call failed")
        return True


@pytest.fixture
def lira(tmp_path):
    adapter = ComLiraAdapter(LiraInstall(2024, "LiraSapr.Application.2024", "", "2024 R2"))
    doc = Doc(str(tmp_path / "m.lir"))
    adapter._app = App(doc)
    return adapter, doc


def test_the_real_adapter_has_every_method_the_fake_one_has():
    import inspect

    from fakes import FakeAdapter

    fake = {n for n, _ in inspect.getmembers(FakeAdapter, inspect.isfunction)}
    real = {n for n, _ in inspect.getmembers(ComLiraAdapter, inspect.isfunction)}
    assert {n for n in fake if not n.startswith("_")} - real == set()


def read(adapter, doc):
    with adapter._reading(doc):
        doc.modified = True  # LIRA marks a document modified by a scratch table


def test_own_reads_do_not_count_as_changes(lira):
    adapter, doc = lira
    read(adapter, doc)
    assert not adapter.document_info().modified


def test_closing_a_read_model_saves_possible_owner_edits(lira):
    adapter, doc = lira
    read(adapter, doc)
    adapter.close_document(doc.PathName)
    assert doc.saves == 1 and doc.closed


def test_discarding_does_not_save(lira):
    adapter, doc = lira
    read(adapter, doc)
    adapter.close_document(doc.PathName, discard=True)
    assert doc.saves == 0 and doc.closed


def test_a_document_that_will_not_close_says_what_to_do(lira, monkeypatch):
    adapter, doc = lira

    def stuck():
        raise OSError("Ошибка.")

    monkeypatch.setattr(doc, "Close", stuck)
    with pytest.raises(LiraComError) as told:
        adapter.close_document(doc.PathName, discard=True)
    text = str(told.value)
    assert "Close failed" in text and "Ошибка." in text and "document 'm'" in text
    assert "close the open documents in LIRA's window by hand" in text


def test_analysis_saves_a_read_model_first(lira):
    adapter, doc = lira
    read(adapter, doc)
    adapter.start_analysis(doc.PathName)
    assert doc.saves == 1
    assert adapter.analysis.status == "running"


def test_failed_start_does_not_leave_analysis_running(lira):
    adapter, doc = lira
    adapter._app.fail = True
    with pytest.raises(LiraComError):
        adapter.start_analysis(doc.PathName)
    assert adapter.analysis.status != "running"


def test_parked_text_files_go_back_unless_the_name_is_taken(tmp_path):
    from lira_sapr_mcp.adapters.com_adapter import return_parked_files

    (tmp_path / "frame.txt.lira-mcp-1a2b3c4d").write_text("owner's text")
    (tmp_path / "slab.txt.lira-mcp-5e6f7a8b").write_text("older")
    (tmp_path / "slab.txt").write_text("newer")
    returned = return_parked_files(tmp_path)
    assert returned == [tmp_path / "frame.txt"]
    assert (tmp_path / "frame.txt").read_text() == "owner's text"
    assert (tmp_path / "slab.txt.lira-mcp-5e6f7a8b").exists()


def test_files_parked_by_a_running_server_process_are_left_to_it(tmp_path):
    import os

    from lira_sapr_mcp.adapters.com_adapter import _process_alive, return_parked_files

    (tmp_path / "live.txt.lira-mcp-111-1a2b3c4d").write_text("being exported")
    (tmp_path / "dead.txt.lira-mcp-222-5e6f7a8b").write_text("crash left it")
    returned = return_parked_files(tmp_path, alive=lambda pid: pid == 111)
    assert returned == [tmp_path / "dead.txt"]
    assert (tmp_path / "live.txt.lira-mcp-111-1a2b3c4d").exists()
    assert _process_alive(os.getpid()) and not _process_alive(4_000_000_000 // 4 * 4)


class Tables:
    def __init__(self, names, stuck=()):
        self.names, self.stuck = list(names), set(stuck)

    @property
    def ItemCount(self):
        return len(self.names)

    def Item(self, i):
        return type("Table", (), {"Name": self.names[i]})()

    def Delete(self, i):
        if self.names[i] in self.stuck:
            raise OSError("LIRA keeps the table")
        del self.names[i]


def test_scratch_tables_of_this_and_ended_processes_are_removed(lira):
    import os

    adapter, doc = lira
    mine, older = f"lira-mcp-{os.getpid()}-aaaa1111", "lira-mcp-1a2b3c4d"
    running, ended, stuck = "lira-mcp-77-bbbb2222", "lira-mcp-88-cccc3333", "lira-mcp-99-dddd"
    doc.AllTables = Tables(["Owner's nodes", mine, older, running, ended, stuck], [stuck])
    left = adapter._drop_tables(doc, alive=lambda pid: pid == 77)
    assert left == [stuck]
    assert doc.AllTables.names == ["Owner's nodes", running, stuck]


def test_a_window_shows_a_document_by_its_whole_name():
    from lira_sapr_mcp.adapters.com_adapter import shows_document

    names = {"frame.lir", "frame"}
    assert shows_document("frame.lir", names) and shows_document("Frame.lir:2", names)
    assert not shows_document("frame2.lir", names)
    assert not shows_document("my frame.lir", names)


def test_files_beside_the_text_are_those_the_export_just_wrote(tmp_path):
    """Stages go to LIRA's data folder under the name entry 40 gives, densities and
    blocks to its work folder; a file older than the export is a stale one of an earlier
    model of the same name."""
    import os

    from lira_sapr_mcp.adapters.com_adapter import stages_file_name, text_companions

    data, work = tmp_path / "Data", tmp_path / "Work"
    data.mkdir(), work.mkdir()
    for f in (data / "m{1}.mnt", work / "m(1).ald", data / "m(1).mnt", data / "other.mnt"):
        f.write_text("x")
    text = '( 0/ 1; m(1)/ 2; 5/ 40;"m{1}.mnt"/ )\n'

    assert stages_file_name("m(1)") == "m{1}.mnt"
    assert text_companions(text, "m(1)", data, work, since=0) == [
        (data / "m{1}.mnt", "m(1).mnt"),
        (work / "m(1).ald", "m(1).ald"),
    ]
    os.utime(work / "m(1).ald", (1000, 1000))
    assert text_companions(text, "m(1)", data, work, 5000) == [(data / "m{1}.mnt", "m(1).mnt")]
    assert text_companions("( 0/ 1; m(1)/ )", "m(1)", data, None, 0) == []


def test_an_export_waits_until_lira_lets_go_of_a_large_text(lira, tmp_path, monkeypatch):
    """LIRA writes a large text for minutes, in parts with pauses, and holds the file
    until it is finished: a size that stood still for a moment is no finished text, and
    a time-out counts from the last time the file grew."""
    adapter, doc = lira
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(tmp_path)})
    target = tmp_path / "m.txt"
    parts = [b"( 0/ 1; m/ )", b"\n( 4/ 0 0 0/ )", b"\n( 1/ 5 1 1/ )"]

    class Writing(App):
        def StartCmd(self, _cmd):
            threading.Thread(target=write_holding, args=(target, parts, 0.6)).start()

    adapter._app = Writing(doc)
    started = time.monotonic()
    assert adapter._export_text(doc, 1.0, None) == b"".join(parts).decode()
    assert time.monotonic() - started > 1.0  # longer than the time-out: it kept growing
    assert not target.exists()


def write_holding(path, parts, pause):
    """Write ``parts`` with pauses, holding the file exclusively, as LIRA does."""
    import win32file

    handle = win32file.CreateFile(
        str(path), win32file.GENERIC_WRITE, 0, None, win32file.CREATE_ALWAYS, 0, None
    )
    try:
        for part in parts:
            win32file.WriteFile(handle, part)
            win32file.FlushFileBuffers(handle)
            time.sleep(pause)
    finally:
        handle.Close()


def test_an_export_stops_when_lira_writes_nothing_new(lira, tmp_path, monkeypatch):
    adapter, doc = lira
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(tmp_path)})
    target = tmp_path / "m.txt"

    class Stuck(App):
        def StartCmd(self, _cmd):
            threading.Thread(target=write_holding, args=(target, [b"( 0/"], 2.5)).start()

    adapter._app = Stuck(doc)
    with pytest.raises(LiraComError, match="nothing new in it for 1 s"):
        adapter._export_text(doc, 1.0, None)


def test_a_failed_export_keeps_the_users_files_aside(lira, tmp_path, monkeypatch):
    """LIRA may still write its text and stages after an export failed: the user's files
    of the same names stay renamed, to be returned when the server starts again."""
    from lira_sapr_mcp.adapters.com_adapter import return_parked_files

    adapter, doc = lira
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(tmp_path)})
    (tmp_path / "m.mnt").write_text("owner's stages")

    class Stuck(App):
        def StartCmd(self, _cmd):
            threading.Thread(target=write_holding, args=(tmp_path / "m.txt", [b"("], 2)).start()

    adapter._app = Stuck(doc)
    with pytest.raises(LiraComError, match="nothing new"):
        adapter._export_text(doc, 1.0, None)
    (tmp_path / "m.mnt").write_text("LIRA's late stages")
    (parked,) = tmp_path.glob("m.mnt.*")
    assert parked.read_text() == "owner's stages"
    (tmp_path / "m.mnt").unlink()
    assert return_parked_files(tmp_path, alive=lambda pid: False) == [tmp_path / "m.mnt"]
    assert (tmp_path / "m.mnt").read_text() == "owner's stages"


def test_users_stages_stay_aside_when_lira_never_wrote_its_own(lira, tmp_path, monkeypatch):
    """The text names a stages file that did not come in time: LIRA may still write it."""
    from lira_sapr_mcp.adapters import com_adapter

    adapter, doc = lira
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(tmp_path)})
    monkeypatch.setattr(com_adapter, "COMPANION_STALL_S", 1.0)
    (tmp_path / "m.mnt").write_text("owner's stages")

    class Writing(App):
        def StartCmd(self, _cmd):
            (tmp_path / "m.txt").write_text('( 0/ 1; m/ 40;"m.mnt"/ )', encoding="utf-8")

    adapter._app = Writing(doc)
    adapter._export_text(doc, 5, None)
    assert not (tmp_path / "m.mnt").exists()
    (parked,) = tmp_path.glob("m.mnt.*")
    assert parked.read_text() == "owner's stages"


def test_an_export_removes_its_stages_file_and_puts_back_the_owners(lira, tmp_path, monkeypatch):
    """The export of a model with stages leaves nothing in LIRA's data folder; copies of
    the stages go where the import will read them, under the text's own name."""
    adapter, doc = lira
    doc.Title = "m(1)"
    data, keep = tmp_path / "Data", tmp_path / "keep"
    data.mkdir(), keep.mkdir()
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(data)})
    (data / "m{1}.mnt").write_text("owner's stages")

    class Writing(App):
        def StartCmd(self, _cmd):
            (data / "m{1}.mnt").write_text("exported stages")
            (data / "m(1).txt").write_text('( 0/ 1; m(1)/ 40;"m{1}.mnt"/ )', encoding="utf-8")

    adapter._app = Writing(doc)
    text = adapter._export_text(doc, 5, None, str(keep))

    assert '40;"m{1}.mnt"' in text
    assert (keep / "m(1).mnt").read_text() == "exported stages"
    assert sorted(p.name for p in data.iterdir()) == ["m{1}.mnt"]
    assert (data / "m{1}.mnt").read_text() == "owner's stages"


def test_an_export_takes_the_files_lira_writes_after_the_text(lira, tmp_path, monkeypatch):
    """LIRA lets go of the text first, then writes <title>.ald in parts and the stages
    file last (the owner's model: 29 MB in 6 s); a copy taken at once was 8 KB of the
    .ald and no stages, and LIRA could not import the text with them."""
    adapter, doc = lira
    data, work, keep = tmp_path / "Data", tmp_path / "Work", tmp_path / "keep"
    for folder in (data, work, keep):
        folder.mkdir()
    monkeypatch.setattr(
        adapter,
        "directories",
        lambda: {"TextDataDirectory": str(data), "WorkDirectory": str(work)},
    )
    ald = [b"blocks " * 1000] * 4

    def lira_writes():
        write_holding(data / "m.txt", [b'( 0/ 1; m/ 40;"m.mnt"/ )'], 0.3)
        write_holding(work / "m.ald", ald, 0.4)
        time.sleep(0.5)
        write_holding(data / "m.mnt", [b"stages"], 0.1)

    class Writing(App):
        def StartCmd(self, _cmd):
            threading.Thread(target=lira_writes).start()

    adapter._app = Writing(doc)
    adapter._export_text(doc, 5, None, str(keep))

    assert (keep / "m.ald").read_bytes() == b"".join(ald)
    assert (keep / "m.mnt").read_bytes() == b"stages"
    assert not (data / "m.mnt").exists()  # removed only once LIRA wrote it


def test_without_stages_an_export_waits_a_little_for_the_ald(tmp_path):
    from lira_sapr_mcp.adapters import com_adapter

    started = time.monotonic()
    assert com_adapter.finished_companions("( 0/ 1; m/ )", "m", tmp_path, tmp_path, 0) == []
    assert com_adapter.COMPANION_GRACE_S <= time.monotonic() - started < 10
    assert com_adapter.finished_companions("( 0/ 1; m/ )", "m", tmp_path, None, 0) == []


def test_an_export_takes_what_there_is_when_lira_writes_no_stages(tmp_path):
    from lira_sapr_mcp.adapters import com_adapter

    (tmp_path / "m.ald").write_text("blocks")
    found = com_adapter.finished_companions('( 40;"m.mnt"/ )', "m", tmp_path, tmp_path, 0, 1.5)
    assert found == [(tmp_path / "m.ald", "m.ald")]


class Exporting(App):
    """LIRA writing the text export: a question while it writes, then the file."""

    def __init__(self, doc, target, asks):
        super().__init__(doc)
        self.target, self.asks, self.questions = target, asks, []

    def StartCmd(self, _cmd):
        self.questions = list(self.asks)


@pytest.fixture
def exporting(lira, tmp_path, monkeypatch):
    """The adapter with a question and a panel it disabled, and LIRA's answers."""
    from lira_sapr_mcp.adapters import com_adapter, dialogs

    adapter, doc = lira
    monkeypatch.setattr(adapter, "directories", lambda: {"TextDataDirectory": str(tmp_path)})
    monkeypatch.setattr(com_adapter, "DIALOG_LOOK_S", 0)
    panel = dialogs.Dialog(1, "Палитра")
    question = dialogs.Dialog(2, "ЛИРА-САПР", ["Заменить?"], ["Да", "Нет"], "#32770")
    app = Exporting(doc, tmp_path / "m.txt", [panel, question])

    def answer_no(d, windows=None):
        if d is not question:
            return False
        app.questions.clear()
        app.target.write_bytes(b"")  # after "No" LIRA leaves the text empty
        return True

    monkeypatch.setattr(dialogs, "find_dialogs", lambda exe, windows=None: list(app.questions))
    monkeypatch.setattr(dialogs, "answer_no", answer_no)
    adapter._app = app
    return adapter, doc, question


def test_a_scratch_export_answers_the_question_no_past_a_panel(exporting):
    adapter, doc, question = exporting
    declined = []
    assert adapter._export_text(doc, 5, declined) == ""
    assert declined == [question.describe()]


def test_an_export_names_the_question_lira_asks(exporting):
    adapter, doc, _ = exporting
    with pytest.raises(LiraInputError, match="Заменить"):
        adapter._export_text(doc, 5, None)


def test_an_export_confirms_the_soil_model_question_recalculating_nothing(exporting, monkeypatch):
    """ "Отменить" would cancel the export of a model tied to a soil model."""
    from lira_sapr_mcp.adapters import dialogs

    adapter, doc, _ = exporting
    text = "С текущей задачей связана модель грунта d:\\soil.sld Выберите требуемые действия"
    soil = dialogs.Dialog(4, "Предупреждение", [text], ["Подтвердить", "Отменить"], "#32770")
    adapter._app.asks = [soil]
    confirmed = []

    def confirm_unchecked(d, windows=None):
        confirmed.append(d.hwnd)
        adapter._app.questions.clear()
        adapter._app.target.write_text("( 0/ 1; m/ )", encoding="utf-8")
        return True

    monkeypatch.setattr(dialogs, "confirm_unchecked", confirm_unchecked)
    assert adapter._export_text(doc, 5, None) == "( 0/ 1; m/ )"
    assert confirmed == [4]


def test_a_scratch_export_stops_at_a_message_it_cannot_decline(exporting):
    from lira_sapr_mcp.adapters import dialogs

    adapter, doc, _ = exporting
    error = dialogs.Dialog(3, "ЛИРА-САПР", ["Ошибка записи"], ["OK"], "#32770")
    adapter._app.asks = [dialogs.Dialog(1, "Палитра"), error]
    with pytest.raises(LiraInputError, match="Ошибка записи"):
        adapter._export_text(doc, 5, [])


def test_the_stages_question_of_an_import_is_answered_yes(tmp_path, monkeypatch):
    """Only LIRA's question about the stages, once, and only with a .mnt beside the text."""
    import time

    from lira_sapr_mcp.adapters import dialogs

    adapter = ComLiraAdapter(LiraInstall(2024, "LiraSapr.Application.2024", "", "2024 R2"))
    other = dialogs.Dialog(1, "Палитра")
    stages = dialogs.Dialog(2, "ЛИРА-САПР", ["Импортировать данные монтажных таблиц?"])
    shown, pressed = [other], []

    def answer_yes(d, windows=None):
        pressed.append(d.hwnd)
        return True

    monkeypatch.setattr(dialogs, "find_dialogs", lambda exe, windows=None: list(shown))
    monkeypatch.setattr(dialogs, "answer_yes", answer_yes)
    src = tmp_path / "m.txt"
    with adapter._watching_import(src):
        shown.append(stages)  # ImportFile runs: LIRA asks
        time.sleep(0.5)
    assert pressed == []
    shown.remove(stages)
    (tmp_path / "m.mnt").write_text("stages")
    with adapter._watching_import(src):
        shown.append(stages)
        deadline = time.monotonic() + 5
        while not pressed and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.7)  # the question stays shown a moment longer: no second press
    assert pressed == [2]
    time.sleep(0.5)
    assert pressed == [2]  # the watcher stopped with the import


@pytest.mark.parametrize("left_behind", [False, True])
def test_a_text_lira_cannot_read_fails_the_import_with_its_message(
    lira, tmp_path, monkeypatch, left_behind
):
    """ImportFile waits until LIRA's error message is closed, then returns a document
    that is not there: the import closes the message and says what LIRA said."""
    from lira_sapr_mcp.adapters import dialogs

    adapter, doc = lira
    if not left_behind:  # the usual case: the document's every call fails

        def close():
            raise OSError("Ошибка.")

        doc.Close = close
    error = dialogs.Dialog(5, "VISOR", ["Неизвестная ошибка при разборе C:\\t\\m.txt"], ["OK"])
    shown, pressed = [], []

    def press_id(d, control_id, windows=None):
        if control_id != dialogs.IDCANCEL:  # LIRA's message box: "ОК" has the id 2
            return False
        pressed.append((d.hwnd, control_id))
        shown.remove(d)
        return True

    def import_file(_path, _template):
        shown.append(error)
        deadline = time.monotonic() + 5
        while error in shown and time.monotonic() < deadline:
            time.sleep(0.05)
        return doc

    monkeypatch.setattr(dialogs, "find_dialogs", lambda exe, windows=None: list(shown))
    monkeypatch.setattr(dialogs, "press_id", press_id)
    adapter._app.ImportFile = import_file
    src = tmp_path / "m.txt"
    src.write_text("( 0/ 1; m /)")
    with pytest.raises(LiraInputError, match=r"m\.txt: Неизвестная ошибка при разборе"):
        adapter.import_file(str(src))
    assert pressed == [(5, dialogs.IDCANCEL)]
    assert doc.closed == left_behind  # a document left behind is not the model asked for
