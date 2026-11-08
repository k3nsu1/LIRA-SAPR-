"""PDF through Word, with Word replaced by a fake: what the caller is told and which
instance gets stopped. No Word is started."""

import threading

import pytest
import pywintypes
import win32com.client

from lira_sapr_mcp.adapters import word
from lira_sapr_mcp.services import slabs as ss

PID = 4242


class FakeDocument:
    def __init__(self, fake):
        self.fake = fake

    def ExportAsFixedFormat(self, path, _format):
        if self.fake.export_error:
            raise self.fake.export_error
        with open(path, "wb") as f:
            f.write(b"%PDF")

    def Close(self, _save):
        self.fake.closing.wait(self.fake.close_hangs_s)


class FakeWord:
    """Word.Application; ``hang_open``: Documents.Open waits until the process is
    stopped, as when Word shows a window of its own."""

    def __init__(self):
        self.export_error = None
        self.hang_open = False
        self.close_hangs_s = 0
        self.stopped = threading.Event()
        self.closing = threading.Event()
        self.alive = {PID}
        self.Documents = self

    def Open(self, *_args):
        if self.hang_open:
            self.stopped.wait(10)
            raise pywintypes.com_error(-2147023174, "RPC server is unavailable", None, None)
        return FakeDocument(self)

    def Quit(self):
        self.alive.discard(PID)


@pytest.fixture
def fake(monkeypatch):
    w = FakeWord()
    started = []

    def dispatch(_name):
        started.append(True)
        return w

    def pids():
        return set(w.alive) if started else set()

    def stop(pid):
        w.stopped.set()
        w.closing.set()
        w.alive.discard(pid)
        w.stopped_pid = pid

    monkeypatch.setattr(win32com.client, "DispatchEx", dispatch)
    monkeypatch.setattr(word, "_word_pids", pids)
    monkeypatch.setattr(word, "_stop", stop)
    monkeypatch.setattr(word, "_dialogs", lambda pid: [])
    return w


def test_word_saves_the_pdf_and_quits(fake, tmp_path):
    pdf = tmp_path / "r.pdf"
    assert word.docx_to_pdf(tmp_path / "r.docx", pdf) == pdf
    assert pdf.read_bytes() == b"%PDF"
    assert fake.alive == set()
    assert not fake.stopped.is_set()  # it quit by itself


def test_no_word_installed_is_lasting(monkeypatch, tmp_path):
    def dispatch(_name):
        raise pywintypes.com_error(word.NO_WORD, "Invalid class string", None, None)

    monkeypatch.setattr(win32com.client, "DispatchEx", dispatch)
    monkeypatch.setattr(word, "_word_pids", set)
    with pytest.raises(word.WordUnavailable, match="not installed") as exc:
        word.docx_to_pdf(tmp_path / "r.docx", tmp_path / "r.pdf")
    assert exc.value.lasting


def test_a_window_of_word_stops_only_our_instance_and_is_lasting(fake, monkeypatch, tmp_path):
    fake.hang_open = True
    monkeypatch.setattr(word, "_dialogs", lambda pid: ["Сбой активации"] if pid == PID else [])
    with pytest.raises(word.WordUnavailable, match="Сбой активации") as exc:
        word.docx_to_pdf(tmp_path / "r.docx", tmp_path / "r.pdf")
    assert exc.value.lasting
    assert fake.stopped_pid == PID


def test_no_answer_in_time_stops_word_and_is_lasting(fake, tmp_path):
    fake.hang_open = True
    with pytest.raises(word.WordUnavailable, match="no answer") as exc:
        word.docx_to_pdf(tmp_path / "r.docx", tmp_path / "r.pdf", timeout_s=0.2)
    assert exc.value.lasting
    assert fake.stopped_pid == PID


def test_an_error_of_one_document_is_not_lasting(fake, tmp_path):
    fake.export_error = pywintypes.com_error(-2146822000, "The file is locked", None, None)
    with pytest.raises(word.WordUnavailable, match="did not save") as exc:
        word.docx_to_pdf(tmp_path / "r.docx", tmp_path / "r.pdf")
    assert not exc.value.lasting
    assert fake.alive == set()  # it still quit


def test_word_that_hangs_closing_keeps_the_pdf_and_is_stopped_soon(fake, monkeypatch, tmp_path):
    fake.close_hangs_s = 30
    monkeypatch.setattr(word, "CLOSE_GRACE_S", 0.2)
    pdf = tmp_path / "r.pdf"
    assert word.docx_to_pdf(tmp_path / "r.docx", pdf) == pdf
    assert fake.stopped_pid == PID


def test_an_error_of_one_document_does_not_turn_word_off_for_the_session(monkeypatch, tmp_path):
    from lira_sapr_mcp.postprocessing.report import Report

    calls = []

    def once_locked(_docx, pdf):
        calls.append(pdf)
        if len(calls) == 1:
            raise word.WordUnavailable("Word did not save the PDF: locked", lasting=False)
        pdf.write_bytes(b"%PDF")
        return pdf

    monkeypatch.setattr(ss.word, "docx_to_pdf", once_locked)
    monkeypatch.setattr(ss, "_word_trouble", [])
    content = Report("Прогибы", ["Модель: m.lir"], ["Плита"], [["1"]], [])
    first = ss.write_reports(content, tmp_path / "a", "pdf")
    second = ss.write_reports(content, tmp_path / "b", "pdf")
    assert first["pdf_by"].startswith("the server's PDF writer: Word did not save")
    assert second["pdf_by"] == "Microsoft Word"
    assert ss._word_trouble == []
