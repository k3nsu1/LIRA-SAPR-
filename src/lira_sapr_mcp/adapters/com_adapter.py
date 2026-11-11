"""Adapter over the LIRA COM API (LiraSapr.Application and newer LiraSapphire.Application).

Every method here must run on the COM worker thread. What a given LIRA release can do is
read from its type library at connect time (``capabilities``), so new methods and input
tables of newer releases show up without code changes.
"""

import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from lira_sapr_mcp.adapters import stamps
from lira_sapr_mcp.adapters.analysis import (
    AnalysisState,
    make_event_handler,
    protocol_path,
    read_protocol,
    summarize_protocol,
)
from lira_sapr_mcp.adapters.base import (
    UNIT_NAMES,
    DocInfo,
    TableError,
    Units,
    bad_model_name,
    decode_lira_text,
    norm_path,
    normalize_rows,
    parse_table_errors,
)
from lira_sapr_mcp.adapters.discovery import LiraInstall, lira_directories
from lira_sapr_mcp.adapters.results import ResultsReader
from lira_sapr_mcp.errors import LiraComError, LiraInputError

log = logging.getLogger(__name__)

TABLE_PREFIX = "kLiraTable_"
# Name of the scratch input tables the server creates and removes again.
TEMP_TABLE_MARK = "lira-mcp-"
NOT_TABLES = {"AllTables", "Group", "Cnt"}
APP_METHODS = (
    "OpenDocument",
    "CreateNewDocument",
    "ImportFile",
    "StartAnalysis",
    "StartAnalysisOnServer",
    "StopAnalysis",
    "DeleteResults",
    "StartZip",
    "StartUnzip",
    "StartCmd",
)
# Menu command "Create text file" (menu resource of Visor.resources.dll).
EXPORT_TEXT_CMD = {2024: 5049}
# Menu "Расчет арматуры > Расчет продавливания" (visor.resources.dll, LIRA 2024 R2).
PUNCHING_CHECK_CMD = {2024: 49595}
DOC_METHODS = (
    "SetGenerateCsvTables",
    "SetDeadWeightLoad",
    "SetSoilModel",
    "Select",
    "GetSelection",
)
# LiraDeadWeightLoadEnum: add to all or selected elements, delete all; LiraSelEnum elements.
DEAD_ALL, DEAD_SELECTED, DEAD_DELETE = 32, 16, -1
SEL_ELEMENTS, SEL_NODES = 0, 1
# LiraCmdIdEnum: projections for pictures, and "fit in window".
VIEWS = {"current": 0, "isometric": 5249, "xoz": 5112, "xoy": 5113}
FIT_IN_WINDOW = 5085
DIALOG_LOOK_S = 1.0  # how often a wait for LIRA looks for a question it asks
# After the text, LIRA writes <title>.ald and then the stages file (a model of 280 000
# elements: 29 MB in 6 s). A file is taken once it stood still this long and is released;
# without stages, the .ald is waited for this long at most; a file that stops growing
# for the last span is taken as it is.
COMPANION_SETTLE_S = 1.0
COMPANION_GRACE_S = 5.0
COMPANION_STALL_S = 120.0
# Besides the text, the export writes construction stages into the data folder under the
# name that entry 40 of document 0 gives ("m{1}.mnt" for model "m(1)"), and densities,
# colours, structural blocks into <title>.ald in the work folder. The import reads both
# from <stem of the text>.mnt and .ald beside the text.
STAGES_ENTRY = re.compile(r'\b40;\s*"([^"/\\]+\.mnt)"', re.IGNORECASE)
# LIRA's question when an imported text has a file of construction stages beside it.
STAGES_QUESTION = "монтажных таблиц"
# LIRA's message when it cannot read an imported text ("Неизвестная ошибка при разборе
# <file>"); ImportFile waits until it is closed and returns a document that is not there.
IMPORT_ERROR = "ошибка"
# LIRA's question on the export of a model tied to a soil model: recalculate the
# foundation's C1/C2 and the piles from it, then "Подтвердить" or "Отменить".
SOIL_QUESTION = "модель грунта"
# Packing the scheme: LIRA's panel and the only options the server packs with (the
# defaults also stitch nodes, round coordinates, repair elements and drop unused data).
PACK_CMD = 5089
PACK_TITLE = "Упаковка"
PACK_OPTIONS = frozenset(
    {"Удаленные узлы и элементы", "Не удалять элементы с некорректной геометрией"}
)
NUMBERED_TABLES = ("Nodes_Coordinates", "Elements_TypeAndNodes")
WM_MDIGETACTIVE = 0x0229


def com_message(exc: BaseException) -> str:
    """Readable text of a pywin32 ``com_error``."""
    args = getattr(exc, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 3 and args[2][2]:
        return str(args[2][2]).strip()
    if len(args) >= 2 and args[1]:
        return str(args[1]).strip()
    return repr(exc)


class ComLiraAdapter:
    def __init__(self, install: LiraInstall) -> None:
        self.install = install
        self._app: Any = None
        self._c: Any = None
        self.capabilities: frozenset[str] = frozenset()
        self.table_kinds: dict[str, int] = {}
        # Documents whose "modified" flag was set only by our own reads: LIRA marks a
        # document modified when a scratch input table is created and removed in it.
        self._dirty_by_reads: set[str] = set()
        self.analysis = AnalysisState()
        self._stamped: float | None = None  # start time of the analysis stamped last
        self._events: Any = None
        self.results = ResultsReader(install)

    # --- connection -----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._app is not None

    def connect(self) -> None:
        """Start or attach to LIRA and read what this release supports."""
        if self._app is not None:
            return
        import win32com.client
        from win32com.client import gencache

        try:
            app = gencache.EnsureDispatch(self.install.progid)
        except Exception as exc:
            raise LiraComError(
                f"Could not start LIRA {self.install.year} ({self.install.progid}): "
                f"{com_message(exc)}. Check that LIRA starts by hand and its license key "
                "is available."
            ) from exc
        self._app = app
        self._c = win32com.client.constants
        names = {k for d in self._c.__dicts__ for k in d}
        self.table_kinds = {
            k[len(TABLE_PREFIX) :]: getattr(self._c, k)
            for k in names
            if k.startswith(TABLE_PREFIX) and k[len(TABLE_PREFIX) :] not in NOT_TABLES
        }
        caps = {m for m in APP_METHODS if hasattr(app, m)}
        caps |= set(DOC_METHODS) & _interface_methods(app, "ILiraDocument")
        self.capabilities = frozenset(caps)
        self._events = win32com.client.WithEvents(app, make_event_handler(self.analysis))
        log.info("connected to %s, capabilities: %s", self.install.progid, sorted(caps))
        dirs = self.directories()
        folder = dirs.get("TextDataDirectory") or dirs.get("DataDirectory")
        if folder:
            return_parked_files(Path(folder))

    def disconnect(self) -> None:
        """Drop our references to LIRA's COM objects (on the COM thread, before it ends).

        LIRA keeps running with its documents; only this server lets go of it.
        """
        import gc

        self._events = None
        self._app = None
        self._c = None
        self.results.close()
        gc.collect()

    def _cast(self, obj: Any, iface: str) -> Any:
        import win32com.client

        return win32com.client.CastTo(obj, iface)

    def _call(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except LiraInputError:
            raise
        except Exception as exc:
            raise LiraComError(f"{what} failed: {com_message(exc)}") from exc

    # --- documents ------------------------------------------------------------------

    @staticmethod
    def _key(doc: Any) -> str:
        return norm_path(str(doc.PathName)) or str(doc.Title).lower()

    def _modified(self, doc: Any) -> bool:
        return bool(doc.IsModified()) and self._key(doc) not in self._dirty_by_reads

    def _maybe_edited(self, doc: Any) -> bool:
        """LIRA says modified, but we counted that as our own reads: the owner may also
        have edited the model in LIRA's window since, and we cannot tell the two apart."""
        return bool(doc.IsModified()) and self._key(doc) in self._dirty_by_reads

    def _save_possible_edits(self, doc: Any) -> None:
        """Save a model whose only known changes are our reads, so that edits the owner
        may have made in LIRA meanwhile are neither lost nor left out of an analysis."""
        if self._maybe_edited(doc) and str(doc.PathName).lower().endswith(".lir"):
            log.info("saving %s before closing or analysing it", doc.PathName)
            path = Path(str(doc.PathName))
            before = path.stat().st_mtime if path.exists() else 0.0
            self._drop_tables(doc)
            self._changed(doc)
            self._call("Save", doc.Save, False)
            work = self._work()
            if work:  # the results of a model we only read stay its results
                stamps.saved_unchanged(work, path, before)

    @contextmanager
    def _reading(self, doc: Any) -> Iterator[None]:
        """Wrap a read that LIRA may count as a change of ``doc``."""
        was_modified = self._modified(doc)
        try:
            yield
        finally:
            if not was_modified:
                self._dirty_by_reads.add(self._key(doc))

    def _changed(self, doc: Any) -> None:
        self._dirty_by_reads.discard(self._key(doc))

    def _info(self, doc: Any, active_path: str | None = None) -> DocInfo:
        path = str(doc.PathName or "")
        if active_path is None:
            active = self._app.ActiveDocument
            active_path = str(active.PathName) if active is not None else ""
        return DocInfo(
            title=str(doc.Title),
            path=path,
            model_type=int(doc.SystemLabel),
            modified=self._modified(doc),
            active=norm_path(path) == norm_path(active_path),
            saved=path.lower().endswith(".lir"),
        )

    def _docs(self) -> list[Any]:
        return [self._app.Document(i) for i in range(self._app.DocumentCount)]

    def list_documents(self) -> list[DocInfo]:
        self.connect()
        active = self._app.ActiveDocument
        active_path = str(active.PathName) if active is not None else ""
        return [self._info(d, active_path) for d in self._docs()]

    def find_document(self, ref: str) -> Any:
        """Find an open document by file path or by title."""
        self.connect()
        docs = self._docs()
        target = norm_path(ref)
        for d in docs:
            if norm_path(str(d.PathName)) == target:
                return d
        by_title = [d for d in docs if str(d.Title).lower() == ref.lower()]
        if len(by_title) == 1:
            return by_title[0]
        open_now = ", ".join(str(d.PathName) for d in docs) or "none"
        raise LiraInputError(f"No open LIRA document matches {ref!r}. Open documents: {open_now}")

    def has_unsaved_changes(self, ref: str) -> bool:
        """Conservative export guard: reads can dirty LIRA's document, and subsequent
        owner edits cannot be distinguished from those reads. Keep the raw flag here."""
        self.connect()
        return bool(self.find_document(ref).IsModified())

    def document_info(self, ref: str | None = None) -> DocInfo:
        """Plain data about ``ref`` (path or title), or about the active document."""
        self.connect()
        if ref:
            return self._info(self.find_document(ref))
        active = self._app.ActiveDocument
        if active is None:
            raise LiraInputError("No model is open in LIRA: open or create one first")
        return self._info(active)

    def open_document(self, path: str) -> tuple[DocInfo, str]:
        self.connect()
        p = Path(path)
        if not p.is_file():
            raise LiraInputError(f"File not found: {path}")
        for d in self._docs():
            if norm_path(str(d.PathName)) == norm_path(str(p)):
                self._call("Activate", d.Activate)
                return self._info(d), "already open"
        if why := bad_model_name(str(p)):
            raise LiraInputError(why)
        res = self._call("OpenDocument", self._app.OpenDocument, str(p.resolve()), False, True)
        doc, msgs = (res if isinstance(res, tuple) else (res, ""))[:2]
        if doc is None:
            raise LiraComError(f"LIRA could not open {path}: {msgs}")
        return self._info(doc), str(msgs or "")

    def create_document(self, title: str, model_type: int, description: str = "") -> DocInfo:
        self.connect()
        bad = set(",.<>()#?*|\\/:")
        if not title or len(title) > 30 or bad & set(title):
            raise LiraInputError(
                "Title must be 1-30 characters without any of , . < > ( ) # ? * | \\ / :"
            )
        doc = self._call(
            "CreateNewDocument",
            self._app.CreateNewDocument,
            title,
            model_type,
            description,
            self._c.kLiraLoadVaksType_Design,
        )
        return self._info(doc)

    def import_file(self, path: str, template: str = "") -> DocInfo:
        self.connect()
        src = Path(path)
        if not src.is_file():
            raise LiraInputError(f"File not found: {path}")
        with self._watching_import(src) as errors:
            doc = self._call("ImportFile", self._app.ImportFile, str(src.resolve()), template)
        if errors:
            # Usually no document is there; when one is, it is not the model asked for.
            if doc is not None:
                with suppress(LiraComError):
                    self._call("Close", doc.Close)
            raise LiraInputError(f"LIRA could not read the text {src.name}: {'; '.join(errors)}")
        return self._info(doc)

    @contextmanager
    def _watching_import(self, src: Path) -> Iterator[list[str]]:
        """ImportFile waits for answers to LIRA's windows, so a watcher thread answers
        them while it runs. With ``<stem>.mnt`` beside the text, LIRA asks whether to
        import the construction stages: "Yes". A text LIRA cannot read ends in an error
        message: the watcher closes it and adds its text to the list it yields. Other
        windows are left alone."""
        import threading

        from lira_sapr_mcp.adapters.dialogs import (
            IDCANCEL,
            IDOK,
            answer_yes,
            find_dialogs,
            press_id,
        )

        exe = self.install.exe
        stages = src.with_suffix(".mnt").is_file()
        answered = {d.hwnd for d in find_dialogs(exe)}
        errors: list[str] = []
        stop = threading.Event()

        def answer(d: Any) -> bool:
            if stages and any(STAGES_QUESTION in t for t in d.text):
                return answer_yes(d)
            if not any(IMPORT_ERROR in t.casefold() for t in d.text):
                return False
            # The only button of a Windows message box, "ОК", has the id of Cancel.
            if press_id(d, IDOK) or press_id(d, IDCANCEL):
                errors.append(" ".join(d.text))
                return True
            return False

        def watch() -> None:
            failed = False
            while not stop.wait(0.3):
                try:
                    for d in find_dialogs(exe):
                        if d.hwnd not in answered and answer(d):
                            answered.add(d.hwnd)
                except Exception:  # a window closing under our feet; look again
                    # Said once: if it keeps failing, the import waits for an answer.
                    log.log(
                        logging.DEBUG if failed else logging.WARNING,
                        "looking for LIRA's windows during an import failed",
                        exc_info=True,
                    )
                    failed = True

        watcher = threading.Thread(target=watch, name="lira-import-stages", daemon=True)
        watcher.start()
        try:
            yield errors
        finally:
            stop.set()
            watcher.join(timeout=2)

    def save_document(self, ref: str, path: str | None = None) -> DocInfo:
        doc = self.find_document(ref)
        self._call("Activate", doc.Activate)
        if path:
            target = Path(path)
            if target.suffix.lower() != ".lir":
                raise LiraInputError("LIRA models are saved with the .lir extension")
            if why := bad_model_name(str(target)):
                raise LiraInputError(why)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._changed(doc)
            self._call("SaveAs", doc.SaveAs, str(target.resolve()), False)
        else:
            if not str(doc.PathName).lower().endswith(".lir"):
                raise LiraInputError("This model has never been saved: give a .lir path")
            self._changed(doc)
            self._call("Save", doc.Save, False)
        return self._info(doc)

    def close_document(self, ref: str, discard: bool = False) -> None:
        """Close a document. LIRA's Close drops unsaved changes, so unless ``discard``
        a saved model that may hold the owner's edits is saved first; callers refuse to
        close models with changes of their own."""
        doc = self.find_document(ref)
        if not discard:
            self._save_possible_edits(doc)
        self._changed(doc)
        title = doc.Title
        try:
            self._call("Close", doc.Close)
        except LiraComError as exc:
            # Seen once, cause unknown: everything after it failed until the owner closed the
            # documents by hand, and "Close failed" alone says nothing of that.
            raise LiraComError(
                f"{exc} LIRA would not close document {title!r}, and later calls may fail too: "
                "close the open documents in LIRA's window by hand and try again"
            ) from exc

    # --- input tables -----------------------------------------------------------------

    def table_kind(self, name: str) -> int:
        key = name[len(TABLE_PREFIX) :] if name.startswith(TABLE_PREFIX) else name
        for k, v in self.table_kinds.items():
            if k.lower() == key.lower():
                return v
        raise LiraInputError(
            f"Unknown input table {name!r}. Known tables: {', '.join(sorted(self.table_kinds))}"
        )

    def _table(self, doc: Any, name: str, numbers: list[int] | None, visible: bool = False) -> Any:
        kind = self.table_kind(name)
        if numbers:
            part, pars = self._c.kLiraModelPart_FromArray, [int(n) for n in numbers]
        elif visible:
            part, pars = self._c.kLiraModelPart_Visible, None
        else:
            part, pars = self._c.kLiraModelPart_All, None
        item = self._call(
            f"create table {name}",
            doc.AllTables.CreateNewItem,
            kind,
            pars,
            part,
            _scratch_name(),
            -1,
        )
        return self._cast(item, "ILiraTable")

    def _drop_tables(self, doc: Any, alive: Callable[[int], bool] | None = None) -> list[str]:
        """Input tables created through the API stay in the document; remove ours, also
        any an earlier failure or an ended server process left. Returns those left.

        Called from ``finally``: a failure here is logged, never raised, so it cannot hide
        the error of the table operation itself.
        """
        alive = alive or _process_alive
        left: list[str] = []
        try:
            tables = doc.AllTables
            for i in range(tables.ItemCount - 1, -1, -1):
                name = str(tables.Item(i).Name)
                if not name.startswith(TEMP_TABLE_MARK) or _held_by_another(name, alive):
                    continue
                try:
                    tables.Delete(i)
                except Exception as exc:
                    log.warning("Could not remove scratch table %r: %s", name, com_message(exc))
                    left.append(name)
        except Exception as exc:
            log.warning("Could not look for scratch tables: %s", com_message(exc))
            left.append(f"{TEMP_TABLE_MARK}*")
        return left

    def remove_scratch_tables(self, ref: str) -> list[str]:
        """Remove the server's scratch tables from a document; returns those left."""
        doc = self.find_document(ref)
        with self._reading(doc):
            return self._drop_tables(doc)

    def read_table(self, ref: str, name: str, numbers: list[int] | None = None) -> list[list]:
        doc = self.find_document(ref)
        with self._reading(doc):
            table = self._table(doc, name, numbers)
            try:
                data = self._call(f"read table {name}", table.GetContents, [])
            finally:
                self._drop_tables(doc)
        return normalize_rows(data)

    def selection(
        self, ref: str, part: str, kind: str | None = None, check_whole: bool = False
    ) -> dict[str, Any]:
        """Numbers of the elements and nodes (only ``kind``, if given) of an open model
        that the user selected in LIRA's window (``part`` "selected") or that LIRA shows,
        e.g. after a fragmentation ("visible"). With ``check_whole``, "whole" says whether
        what is visible is all of the model. The user's selection is left as it is."""
        doc = self.find_document(ref)
        kinds = [k for k in ("elements", "nodes") if kind in (None, k)]
        if part == "selected":
            sel = {"elements": SEL_ELEMENTS, "nodes": SEL_NODES}
            return {
                k: sorted(int(n) for n in self._call("GetSelection", doc.GetSelection, [], sel[k]))
                for k in kinds
            }
        if part != "visible":
            raise LiraInputError(f'part must be "selected" or "visible", not {part!r}')
        tables = {"elements": NUMBERED_TABLES[1], "nodes": NUMBERED_TABLES[0]}
        found: dict[str, Any] = {}
        with self._reading(doc):
            try:
                for k in kinds:
                    found[k] = self._numbers_shown(doc, tables[k], visible=True)
                if check_whole:
                    found["whole"] = all(
                        len(found[k]) == len(self._numbers_shown(doc, tables[k])) for k in kinds
                    )
            finally:
                self._drop_tables(doc)
        return found

    def _numbers_shown(self, doc: Any, name: str, visible: bool = False) -> list[int]:
        """Row numbers of a node or element table, of the visible part or of all."""
        table = self._table(doc, name, None, visible=visible)
        try:
            data = table.GetContents([])
        except Exception as exc:  # LIRA fails on an empty table: nothing is shown
            log.debug("reading %s gave %s: taken as empty", name, com_message(exc))
            data = None
        return sorted(int(row[0]) for row in normalize_rows(data) if row and row[0] is not None)

    def write_table(
        self, ref: str, name: str, rows: list[list], numbers: list[int] | None = None
    ) -> list[TableError]:
        """Replace the rows of a table (for ``numbers`` only, if given) and apply them."""
        doc = self.find_document(ref)
        self._changed(doc)
        table = self._table(doc, name, numbers)
        try:
            data = [["" if cell is None else cell for cell in row] for row in rows]
            self._call(f"fill table {name}", table.SetContents, data)
            ok, errs = self._call(f"apply table {name}", table.Apply)
        finally:
            self._drop_tables(doc)
        errors = parse_table_errors(str(errs or ""))
        if not ok and not errors:
            errors = [TableError(-1, -1, "LIRA rejected the table without details")]
        return errors

    def self_weight(
        self, ref: str, load_case: int, factor: float, elements: list[int] | None = None
    ) -> str:
        """Add self weight loads (of elements whose sections have a density) to a load
        case, which LIRA creates if needed; only ``elements`` if given. Returns LIRA's
        message, if any."""
        doc = self.find_document(ref)
        self._changed(doc)
        doc.CurrentLoadCase = load_case
        if not elements:
            return str(self._call("SetDeadWeightLoad", doc.SetDeadWeightLoad, DEAD_ALL, factor))
        # LIRA loads the selected elements: select only ours, then give the user back
        # the selection they had in LIRA's window (a LIRA without GetSelection loses it).
        users = (
            list(self._call("GetSelection", doc.GetSelection, [], SEL_ELEMENTS))
            if "GetSelection" in self.capabilities
            else []
        )
        self._call("Unselect", doc.Unselect, "all", SEL_ELEMENTS)
        self._call("Select", doc.Select, " ".join(map(str, elements)), SEL_ELEMENTS)
        try:
            return str(
                self._call("SetDeadWeightLoad", doc.SetDeadWeightLoad, DEAD_SELECTED, factor)
            )
        finally:
            try:
                self._call("Unselect", doc.Unselect, "all", SEL_ELEMENTS)
                if users:
                    self._call("Select", doc.Select, " ".join(map(str, users)), SEL_ELEMENTS)
            except LiraComError:
                log.warning("Could not give the user's selection back after adding self weight")

    def model_picture(self, ref: str, view: str = "isometric") -> tuple[int, int, bytes]:
        """Width, height and BGRA pixels of LIRA's view window showing the model in the
        given projection (or the current one), fitted to the window. ``picture_png``
        makes the PNG, off the COM thread."""
        import win32gui

        from lira_sapr_mcp.adapters.picture import capture_window

        if view not in VIEWS:
            raise LiraInputError(f"Unknown view {view!r}; use one of {sorted(VIEWS)}")
        doc = self.find_document(ref)
        self._call("Activate", doc.Activate)
        for cmd in (VIEWS[view], FIT_IN_WINDOW):
            if cmd:
                self._call("StartCmd", self._app.StartCmd, cmd)
                self._settle(0.6)  # LIRA redraws after handling its window messages
        frame = self._view_window(doc)
        if win32gui.IsIconic(win32gui.GetAncestor(frame, 2)):  # GA_ROOT
            raise LiraInputError("LIRA's window is minimized: restore it to take a picture")
        try:
            return capture_window(frame)
        except ValueError as exc:
            raise LiraComError(f"Could not take a picture of {doc.Title}: {exc}") from exc

    def _view_window(self, doc: Any) -> int:
        """The largest drawing window inside the active window of LIRA's MDI area (the
        caller activates the document), if that window is the document's."""
        import win32gui

        from lira_sapr_mcp.adapters.dialogs import Win32Windows

        names = {Path(str(doc.PathName)).name, str(doc.Title)}
        windows = Win32Windows()
        for top in windows.top_level(Path(self.install.exe).name):
            for mdi in windows.children(top):
                if win32gui.GetClassName(mdi) != "MDIClient":
                    continue
                frame = win32gui.SendMessage(mdi, WM_MDIGETACTIVE, 0, 0)
                if frame and shows_document(win32gui.GetWindowText(frame), names):
                    inner = windows.children(frame) or [frame]
                    return max(inner, key=lambda h: _area(win32gui.GetClientRect(h)))
        raise LiraComError(f"LIRA shows no window of {doc.Title}")

    def _settle(self, seconds: float) -> None:
        import pythoncom

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)

    def script_names(self, ref: str | None) -> dict[str, Any]:
        """LIRA's objects for the agent's script (services/scripts.py): the document of
        ``ref``, default the active one, counts as changed."""
        self.connect()
        doc = self.find_document(ref) if ref else self._app.ActiveDocument
        if doc is not None:
            self._changed(doc)  # the script may change it: never treat it as only read
        return {"lira": self._app, "doc": doc, "adapter": self, "constants": self._c}

    def delete_self_weight(self, ref: str) -> str:
        doc = self.find_document(ref)
        self._changed(doc)
        return str(self._call("SetDeadWeightLoad", doc.SetDeadWeightLoad, DEAD_DELETE, 1.0))

    # --- text format ----------------------------------------------------------------

    def directories(self) -> dict[str, str]:
        return lira_directories(self.install.year)

    def _work(self) -> Path | None:
        work = self.directories().get("WorkDirectory")
        return Path(work) if work else None

    def export_text(
        self,
        ref: str,
        timeout_s: float = 120.0,
        declined: list[str] | None = None,
        companions_to: str | None = None,
    ) -> str:
        """The model in LIRA's text format, via the menu command "Create text file".

        LIRA writes ``<text data folder>/<title>.txt`` and, for a model with
        construction stages, a stages file beside it; both are removed afterwards, and
        files of the same names that were already there are put back. If LIRA asks a
        question meanwhile, the call stops at once and names it; with ``declined`` (for
        the server's own scratch models) the server answers "No" and notes the question
        there. With ``companions_to``, copies of the stages and ``<title>.ald`` files the
        export wrote go into that folder as ``<title>.mnt`` and ``<title>.ald``, the names
        LIRA's import reads beside a text ``<title>.txt``.
        """
        doc = self.find_document(ref)
        with self._reading(doc):
            return self._export_text(doc, timeout_s, declined, companions_to)

    def _export_text(
        self,
        doc: Any,
        timeout_s: float,
        declined: list[str] | None,
        companions_to: str | None = None,
    ) -> str:
        import pythoncom

        from lira_sapr_mcp.adapters.dialogs import (
            DIALOG_CLASS,
            answer_no,
            confirm_unchecked,
            find_dialogs,
        )

        doc.Activate()
        dirs = self.directories()
        folder = dirs.get("TextDataDirectory") or dirs.get("DataDirectory")
        if not folder:
            raise LiraComError("LIRA's data folder is not set in its settings (registry)")
        title = str(doc.Title)
        target = Path(folder) / f"{title}.txt"
        stages = Path(folder) / stages_file_name(title)
        exe = self.install.exe
        before = {d.hwnd for d in find_dialogs(exe)}
        started = time.time()
        parked, parked_stages = _set_aside(target), _set_aside(stages)
        finished = False
        try:
            cmd = EXPORT_TEXT_CMD.get(self.install.year, EXPORT_TEXT_CMD[2024])
            self._call("StartCmd(Create text file)", self._app.StartCmd, cmd)
            # A large text takes minutes (92 MB: 4 min): the time-out counts from the
            # start until the file appears, then from the last time it grew.
            progress = time.monotonic()
            last_size, look_at = -1, time.monotonic() + DIALOG_LOOK_S
            while True:
                pythoncom.PumpWaitingMessages()
                if time.monotonic() > look_at:
                    look_at = time.monotonic() + DIALOG_LOOK_S
                    new = [d for d in find_dialogs(exe) if d.hwnd not in before]
                    # A model tied to a soil model: "Отменить" would cancel the export,
                    # so it is confirmed with nothing recalculated (owner's decision).
                    for asked in [d for d in new if SOIL_QUESTION in " ".join(d.text)]:
                        if confirm_unchecked(asked):
                            log.info("confirmed without recalculation: %s", asked.describe())
                            before.add(asked.hwnd)
                            new.remove(asked)
                    # Scratch models: questions get "No"; a panel that a question disabled
                    # along with LIRA's main window is no question; a message box without
                    # "No" (an error) stops the export.
                    if declined is not None:
                        answered = [d for d in new if answer_no(d)]
                        declined.extend(d.describe() for d in answered)
                        before.update(
                            d.hwnd for d in new if d in answered or d.kind != DIALOG_CLASS
                        )
                        new = [d for d in new if d.hwnd not in before]
                    if new:
                        raise LiraInputError(
                            f"LIRA asks a question while writing {doc.Title} as text and "
                            f"waits for an answer: {'; '.join(d.describe() for d in new)}. "
                            "Answer it with lira_press_dialog_button or ask the user to, "
                            "then repeat the call"
                        )
                if target.exists():
                    size = target.stat().st_size
                    if size != last_size:
                        last_size, progress = size, time.monotonic()
                    # LIRA holds the file until the text is finished; after a "No" it
                    # may leave it empty.
                    elif (size > 0 or declined) and _released(target):
                        break
                if time.monotonic() - progress > timeout_s:
                    raise LiraComError(
                        f"LIRA did not write the text export {target}: nothing new in it for "
                        f"{timeout_s:.0f} s"
                    )
                time.sleep(0.2)
            text = decode_lira_text(target.read_bytes())
            if text is None:
                raise LiraComError(f"Cannot decode LIRA's text export {target}")
            # Waited for even when not wanted: the stages file is removed below.
            found = finished_companions(text, title, Path(folder), self._work(), started)
            # Without the stages file it gave up waiting: LIRA may still write it.
            finished = STAGES_ENTRY.search(text) is None or any(
                name.endswith(".mnt") for _, name in found
            )
            if companions_to:
                for file, name in found:
                    shutil.copyfile(file, Path(companions_to) / name)
        finally:
            for file, aside in ((target, parked), (stages, parked_stages)):
                if not _remove(file) or aside is None:
                    continue
                if finished:
                    aside.rename(file)
                else:  # LIRA may still write its file over the user's: keep it aside
                    log.warning(
                        "The export of %s failed; the user's file %s stays as %s until the "
                        "server starts again",
                        title,
                        file,
                        aside.name,
                    )
        return text

    def numbering_gaps(self, ref: str) -> dict[str, list[int]]:
        """Node and element tables whose numbers have gaps, as [rows, highest number]:
        the scheme is not packed, and deleted nodes and elements keep their numbers."""
        gaps = {}
        for name in NUMBERED_TABLES:
            rows = self.read_table(ref, name)
            top = max((int(r[0]) for r in rows), default=0)
            if top != len(rows):
                gaps[name] = [len(rows), top]
        return gaps

    def pack_scheme(self, ref: str, timeout_s: float = 600.0) -> None:
        """Pack the scheme through LIRA's panel "Упаковка" with only "deleted nodes and
        elements" on: they go, and the remaining nodes and elements are numbered without
        gaps. Nothing is stitched, rounded, repaired or removed as unused, and LIRA does
        not save the model by itself."""
        import pythoncom

        from lira_sapr_mcp.adapters.dialogs import IDCANCEL, IDOK, Win32Windows, set_options

        doc = self.find_document(ref)
        w, exe = Win32Windows(), Path(self.install.exe).name
        before = set(w.top_level(exe))
        self._call("Activate", doc.Activate)
        self._call("StartCmd(Pack the scheme)", self._app.StartCmd, PACK_CMD)
        deadline = time.monotonic() + 20
        panel = None
        while panel is None:
            pythoncom.PumpWaitingMessages()
            panel = next(
                (h for h in w.top_level(exe) if h not in before and w.text(h) == PACK_TITLE), None
            )
            if panel is None and time.monotonic() > deadline:
                raise LiraComError("LIRA did not open its packing panel (command 5089)")
            time.sleep(0.3)
        try:
            if not set_options(panel, PACK_OPTIONS, w):
                raise LiraComError(
                    f"LIRA's packing panel has no options {sorted(PACK_OPTIONS)}: this "
                    "release of LIRA is not supported for packing"
                )
            w.click(next(c for c in w.children(panel) if w.control_id(c) == IDOK))
            deadline = time.monotonic() + timeout_s
            while self.numbering_gaps(ref):
                pythoncom.PumpWaitingMessages()
                if time.monotonic() > deadline:
                    raise LiraComError(f"LIRA did not pack {doc.Title} within {timeout_s:.0f} s")
                time.sleep(1)
        finally:
            # Changed even when the wait failed: a half-packed model is no read.
            self._changed(doc)
            for c in w.children(panel) if panel in w.top_level(exe) else []:
                if w.control_id(c) == IDCANCEL:
                    w.click(c)

    def punching_check(self, ref: str, timeout_s: float = 120.0) -> dict[str, Any]:
        """Run LIRA's punching shear check of an analysed model, as its menu command does,
        and wait until LIRA writes the loads on punching contours.

        The command returns at once and LIRA works on in its window; it shows nothing
        when the model has no contours or no results, so only a timeout tells.
        """
        import pythoncom

        doc = self.find_document(ref)
        path = Path(str(doc.PathName))
        work = self._work()
        if work is None:
            raise LiraComError("LIRA's work folder is not set in its settings (registry)")
        main = stamps.results_marker(work, path.stem)
        if not main.exists():
            raise LiraInputError(f"{path.name} has no analysis results: run lira_run_analysis")
        loads = stamps.punching_loads_file(work, path.stem)
        loads_before = loads.stat().st_mtime if loads.exists() else None
        written_before = main.stat().st_mtime
        started = time.monotonic()
        with self._reading(doc):
            self._call("Activate", doc.Activate)
            cmd = PUNCHING_CHECK_CMD.get(self.install.year, PUNCHING_CHECK_CMD[2024])
            self._call("StartCmd(Punching shear check)", self._app.StartCmd, cmd)
            last = None
            while True:
                pythoncom.PumpWaitingMessages()
                now = loads.stat().st_mtime if loads.exists() else None
                if now is not None and now != loads_before and now == last:
                    break  # written and not changed since the previous look
                last = now
                if time.monotonic() - started > timeout_s:
                    raise LiraInputError(
                        f"LIRA wrote no loads on punching contours of {path.name} within "
                        f"{timeout_s:.0f} s: the model may have no punching contours or no "
                        "reinforced concrete design data of plates and columns, or LIRA "
                        "waits in a dialog (lira_dialogs)"
                    )
                time.sleep(0.5)
        stamps.results_rewritten(work, path, written_before)
        return {"model": str(path), "elapsed_s": round(time.monotonic() - started, 1)}

    # --- analysis -------------------------------------------------------------------

    def start_analysis(self, ref: str) -> dict:
        """Start analysing a saved model; returns at once (progress comes as events)."""
        info = self.document_info(ref)
        if not info.saved:
            raise LiraInputError("Save the model to a .lir file before the analysis")
        if info.modified:
            raise LiraInputError(
                f"{info.title} has unsaved changes; LIRA analyses the file on disk: save first"
            )
        if self.analysis.status in ("running", "needs_user"):
            raise LiraInputError(f"An analysis of {self.analysis.model} is still running")
        self._save_possible_edits(self.find_document(info.path))
        self.analysis.start(info.path)
        try:
            ok = self._call("StartAnalysis", self._app.StartAnalysis, info.path)
        except BaseException:
            self.analysis.on_finished(1)
            raise
        if not ok:
            self.analysis.on_finished(1)
            work = self.directories().get("WorkDirectory", "")
            log_text = read_protocol(Path(work) / "PackageAnalysisLog.TXT") or ""
            raise LiraComError(f"LIRA refused to start the analysis. {log_text.strip()}")
        return self.analysis.as_dict()

    def _stamp_finished(self) -> None:
        """Stamp the results of the server's last analysis once it has finished."""
        a = self.analysis
        if a.status == "finished" and a.model and self._stamped != a.started_at:
            work = self._work()
            if work:
                stamps.write_stamp(work, Path(a.model))
            self._stamped = a.started_at

    def analysis_status(self) -> dict:
        self._stamp_finished()
        status = self.analysis.as_dict()
        if self.analysis.done and self.analysis.model:
            text = self.protocol(self.analysis.model)
            if text is not None:
                status["protocol"] = summarize_protocol(text)
            status["model_closed_by_lira"] = not any(
                norm_path(str(d.PathName)) == norm_path(self.analysis.model) for d in self._docs()
            )
        return status

    def stop_analysis(self) -> None:
        self._call("StopAnalysis", self._app.StopAnalysis)

    def results_info(self, model_path: str) -> dict[str, Any]:
        """Whether LIRA keeps analysis results under this model's name, their size and time,
        and a warning when they may belong to an older version of the model or to another
        model of the same name (see stamps)."""
        main = self.results.main_data(model_path)
        if main is None:
            return {"present": False}
        info: dict[str, Any] = {"present": True, **main}
        work = self._work()
        marker = stamps.results_marker(work, Path(model_path).stem) if work else None
        if marker and marker.exists():
            written = datetime.fromtimestamp(marker.stat().st_mtime)
            info["written"] = written.isoformat(timespec="seconds")
        warning = self.results_warning(model_path)
        if warning:
            info["warning"] = warning
        return info

    def results_warning(self, model_path: str) -> str | None:
        """Why the results under this model's name may not be its own, or None."""
        self._stamp_finished()
        work = self._work()
        return stamps.results_warning(work, Path(model_path)) if work else None

    def delete_results(self, model_path: str) -> None:
        if (
            self.analysis.status in ("running", "needs_user")
            and self.analysis.model
            and (norm_path(self.analysis.model) == norm_path(model_path))
        ):
            raise LiraInputError(f"The analysis of {model_path} is still running")
        self._call("DeleteResults", self._app.DeleteResults, model_path)
        work = self._work()
        if work:
            stamps.remove_stamp(work, Path(model_path).stem)

    def protocol(self, model_path: str) -> str | None:
        work = self.directories().get("WorkDirectory")
        return read_protocol(protocol_path(work, model_path)) if work else None

    # --- results --------------------------------------------------------------------

    def node_displacements(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        return self.results.node_displacements(model_path, nodes, cases)

    def vertical_displacements(
        self,
        model_path: str,
        nodes: list[int],
        case: int | None = None,
        combination: int | None = None,
    ) -> dict[int, float]:
        return self.results.vertical_displacements(model_path, nodes, case, combination)

    def element_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        cases: list[int] | None,
        families: list[str] | None = None,
    ) -> list[dict]:
        return self.results.element_forces(model_path, elements, cases, families)

    def load_combinations(self, model_path: str) -> dict[str, Any]:
        return self.results.load_combinations(model_path)

    def combination_displacements(
        self, model_path: str, nodes: list[int] | None, combinations: list[int] | None
    ) -> list[dict]:
        return self.results.combination_displacements(model_path, nodes, combinations)

    def combination_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        combinations: list[int] | None,
        families: list[str] | None = None,
        limit_states: list[int] | None = None,
    ) -> list[dict]:
        return self.results.combination_forces(
            model_path, elements, combinations, families, limit_states
        )

    def design_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        families: list[str] | None = None,
        limit_states: list[int] | None = None,
    ) -> list[dict]:
        return self.results.design_forces(model_path, elements, families, limit_states)

    def periods(self, model_path: str, cases: list[int] | None) -> list[dict]:
        return self.results.periods(model_path, cases)

    def reinforcement(
        self, model_path: str, elements: list[int] | None, design_option: int = 1
    ) -> list[dict]:
        return self.results.reinforcement(model_path, elements, design_option)

    def plate_reinforcement(
        self,
        model_path: str,
        elements: list[int],
        components: list[str],
        design_option: int = 1,
    ) -> dict[str, dict[int, float]]:
        return self.results.plate_reinforcement(model_path, elements, components, design_option)

    def fragment_loads(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        return self.results.fragment_loads(model_path, nodes, cases)

    def punching_loads(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        work = self._work()
        if work and stamps.punching_outdated(work, Path(model_path).stem):
            raise LiraInputError(
                f"The loads on punching contours of {Path(model_path).stem!r} are from a "
                "punching shear check run before the last analysis: run lira_punching_check"
            )
        return self.results.punching_loads(model_path, nodes, cases)

    # --- units ----------------------------------------------------------------------

    def units(self) -> Units:
        self.connect()
        u = self._app.MeasurementUnits

        def n(kind: str, value: int) -> str:
            names = UNIT_NAMES[kind]
            return names[value] if 0 <= value < len(names) else "none"

        return Units(
            geometry=n("geometry", u.Geometry),
            sections=n("geometry", u.Sections),
            loads=n("force", u.Loads1),
            loads_per=n("geometry", u.Loads2),
            materials=n("stress", u.MaterialProperties1),
            materials_per=n("geometry", u.MaterialProperties2),
            displacements=n("geometry", u.Displacements),
            forces=n("force", u.Forces1),
            forces_per=n("geometry", u.Forces2),
            stresses=n("stress", u.Stresses1),
            stresses_per=n("geometry", u.Stresses2),
            temperature=n("temperature", u.Temperature),
        )


def _scratch_name() -> str:
    """Name of a scratch table or suffix of a set-aside file: names this process, so that
    another server process sharing LIRA leaves it alone while this one is alive."""
    return f"{TEMP_TABLE_MARK}{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _held_by_another(tag: str, alive: Callable[[int], bool]) -> bool:
    """Whether a scratch name (``lira-mcp-<pid>-<id>``) belongs to another server process
    that still runs; names without a process are leftovers of older versions."""
    owner, _, rest = tag.removeprefix(TEMP_TABLE_MARK).partition("-")
    return bool(rest) and owner.isdigit() and int(owner) != os.getpid() and alive(int(owner))


def _process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def stages_file_name(title: str) -> str:
    """The name LIRA's text export gives the stages file of model ``title``."""
    return title.replace("(", "{").replace(")", "}") + ".mnt"


def finished_companions(
    text: str,
    title: str,
    data: Path,
    work: Path | None,
    since: float,
    stall_s: float | None = None,
) -> list[tuple[Path, str]]:
    """``text_companions`` once LIRA has finished them: it writes them only after it lets
    go of the text, the stages file last. Waits for the stages file the text names, then
    until every file found stands still and is released."""
    stall_s = COMPANION_STALL_S if stall_s is None else stall_s
    expects_stages = STAGES_ENTRY.search(text) is not None
    if not expects_stages and work is None:
        return []
    started = progress = time.monotonic()
    last: list[tuple[Path, int]] | None = None
    while True:
        try:
            found = text_companions(text, title, data, work, since)
            sizes = [(f, f.stat().st_size) for f, _ in found]
        except OSError:  # a file replaced under our feet: look again
            if time.monotonic() - progress > stall_s:
                log.warning("LIRA's files beside the text of %s stay unreadable", title)
                return []
            time.sleep(0.2)
            continue
        now = time.monotonic()
        if sizes != last:
            last, progress = sizes, now
        has_stages = any(name.endswith(".mnt") for _, name in found)
        complete = has_stages or (
            not expects_stages and (found or now - started >= COMPANION_GRACE_S)
        )
        settled = complete and now - progress >= COMPANION_SETTLE_S
        if settled and all(_released(f) for f, _ in found):
            return found
        if now - progress > stall_s:
            log.warning(
                "LIRA wrote nothing beside the text of %s for %.0f s; taking %s",
                title,
                stall_s,
                [f.name for f, _ in found],
            )
            return [(f, name) for f, name in found if _released(f)]
        time.sleep(0.2)


def text_companions(
    text: str, title: str, data: Path, work: Path | None, since: float
) -> list[tuple[Path, str]]:
    """Files a text export of model ``title`` wrote at or after ``since``
    (``time.time()``), with the names LIRA's import reads them by beside ``<title>.txt``:
    the stages file that entry 40 of the text names, and ``<title>.ald``."""
    found = []
    entry = STAGES_ENTRY.search(text)
    if entry:
        found.append((data / entry.group(1), f"{title}.mnt"))
    if work is not None:
        found.append((work / f"{title}.ald", f"{title}.ald"))
    # A second of slack for the file system's clock; an older file is stale.
    return [(f, name) for f, name in found if f.is_file() and f.stat().st_mtime >= since - 1]


def _released(file: Path) -> bool:
    """Whether no process holds ``file`` open: LIRA holds a text export until it is
    finished (a large text is written for minutes, with pauses)."""
    import pywintypes
    import win32file

    try:
        handle = win32file.CreateFile(
            str(file), win32file.GENERIC_READ, 0, None, win32file.OPEN_EXISTING, 0, None
        )
    except pywintypes.error:
        return False
    handle.Close()
    return True


def _remove(file: Path, wait_s: float = 60.0) -> bool:
    """Delete ``file``, waiting for LIRA to let go of it (a cancelled export may still be
    writing); False when it is still held, so that nothing is renamed onto it."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            file.unlink(missing_ok=True)
            return True
        except PermissionError:
            if time.monotonic() > deadline:
                log.warning("%s is still held by LIRA and was left in place", file)
                return False
            time.sleep(0.5)


def _set_aside(file: Path) -> Path | None:
    """Rename an existing ``file`` out of LIRA's way; ``return_parked_files`` puts back
    what a crash leaves renamed."""
    if not file.exists():
        return None
    parked = file.with_name(f"{file.name}.{_scratch_name()}")
    file.rename(parked)
    return parked


def return_parked_files(folder: Path, alive: Callable[[int], bool] = _process_alive) -> list[Path]:
    """Put back the user's text and stages files that a text export set aside and a
    crash left renamed (``<name>.txt.lira-mcp-<pid>-<id>``). Files of a server process still
    running are its own business; a file whose name is taken again is left and logged."""
    returned = []
    for parked in folder.glob(f"*.{TEMP_TABLE_MARK}*"):
        original_name, _, tag = parked.name.partition(f".{TEMP_TABLE_MARK}")
        owner = tag.split("-")[0]
        if "-" in tag and owner.isdigit() and alive(int(owner)):
            continue
        original = parked.with_name(original_name)
        if original.exists():
            log.warning("Parked file %s not returned: %s exists again", parked, original.name)
            continue
        try:
            parked.rename(original)
            returned.append(original)
            log.warning("Returned the parked file %s", original)
        except OSError as exc:
            log.warning("Could not return the parked file %s: %s", parked, exc)
    return returned


def _interface_methods(app: Any, interface: str) -> set[str]:
    """Method names of an interface in LIRA's type library (the release's own API).

    Read from the library itself: makepy's generated module for the application does not
    hold the other interfaces.
    """
    try:
        tlib, _ = app._oleobj_.GetTypeInfo().GetContainingTypeLib()
        for i in range(tlib.GetTypeInfoCount()):
            if tlib.GetDocumentation(i)[0] == interface:
                info = tlib.GetTypeInfo(i)
                count = info.GetTypeAttr().cFuncs
                return {info.GetNames(info.GetFuncDesc(k).memid)[0] for k in range(count)}
    except Exception as exc:  # pywintypes.com_error: an old or odd type library
        log.warning("Could not read %s from LIRA's type library: %s", interface, exc)
    return set()


def shows_document(title: str, names: set[str]) -> bool:
    """Whether a window titled ``title`` ("frame.lir", or "frame.lir:2" for a second
    window) is the document's: whole names only, so "frame" is not "frame2"."""
    base = re.sub(r":\d+$", "", title.strip()).casefold()
    return base in {n.casefold() for n in names if n}


def _area(rect: tuple[int, int, int, int]) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])
