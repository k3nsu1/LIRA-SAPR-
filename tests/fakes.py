"""An in-memory stand-in for ComLiraAdapter, so services and tools run without LIRA."""

import re
import shutil
from pathlib import Path

from lira_sapr_mcp.adapters.base import DocInfo, TableError, Units
from lira_sapr_mcp.adapters.dialogs import Dialog
from lira_sapr_mcp.adapters.discovery import LiraInstall
from lira_sapr_mcp.com.worker import ComWorker
from lira_sapr_mcp.config import Settings
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.services.textformat import TextModel

INSTALL = LiraInstall(2024, "LiraSapr.Application.2024", r"C:\LIRA\LiraSapr.exe", "24.2.3.1")


class FakeAdapter:
    def __init__(self, install: LiraInstall = INSTALL) -> None:
        self.install = install
        self.connected = False
        self.capabilities = frozenset({"OpenDocument", "CreateNewDocument", "ImportFile",
                                       "StartAnalysis", "StopAnalysis", "DeleteResults",
                                       "StartCmd", "SetDeadWeightLoad", "Select",
                                       "GetSelection"})  # fmt: skip
        self.table_kinds = {"Nodes_Coordinates": 2, "Elements_TypeAndNodes": 3, "LoadCases": 25,
                            "Nodes_Restraints": 4, "Stiffnesses": 9, "Elements_Stiffnesses": 10,
                            "Stiffnesses_Factors": 28}  # fmt: skip
        self.docs: dict[str, dict] = {}
        self.active: str | None = None
        self.analysis = "idle"
        self.analysed: str | None = None
        self.deleted: set[str] = set()
        self.results_warnings: dict[str, str] = {}  # model path -> stale results warning
        self.dialogs_shown: list[Dialog] = []  # what LIRA's windows would show
        self.stuck_tables: list[str] = []  # scratch tables LIRA would not delete
        self.companions: dict[str, list[str]] = {}  # model title -> files its export writes
        self.imported_beside: list[str] = []  # files in the folder of the last import
        self.slab_w: dict[int, float] = {}  # node -> Z displacement (m) for slabs
        self.plate_as: dict[str, dict[int, float]] = {}  # component -> element -> cm2/m
        self.units_geometry = "m"

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def _get(self, ref: str | None) -> dict:
        key = ref or self.active
        for k, d in self.docs.items():
            if key in (k, d["title"]):
                return d
        raise LiraInputError(f"No open LIRA document matches {ref!r}")

    def _info(self, d: dict) -> DocInfo:
        return DocInfo(
            d["title"],
            d["path"],
            d["type"],
            d["modified"],
            d["path"] == self.active,
            d["path"].endswith(".lir"),
        )

    def list_documents(self):
        return [self._info(d) for d in self.docs.values()]

    def document_info(self, ref=None):
        if not self.docs:
            raise LiraInputError("No model is open in LIRA: open or create one first")
        return self._info(self._get(ref))

    def open_document(self, path):
        if not Path(path).is_file():
            raise LiraInputError(f"File not found: {path}")
        d = {"title": Path(path).stem, "path": path, "type": 5, "modified": False, "tables": {}}
        self.docs[path] = d
        self.active = path
        return self._info(d), ""

    def create_document(self, title, model_type, description=""):
        path = rf"C:\LIRA\Data\{title}"
        d = {"title": title, "path": path, "type": model_type, "modified": True, "tables": {}}
        self.docs[path] = d
        self.active = path
        return self._info(d)

    def save_document(self, ref, path=None):
        d = self._get(ref)
        if path:
            self.docs.pop(d["path"])
            d["path"] = path
            self.docs[path] = d
            self.active = path
            Path(path).write_bytes(b"lir")
        d["modified"] = False
        return self._info(d)

    def close_document(self, ref, discard=False):
        d = self._get(ref)
        self.docs.pop(d["path"])
        if self.active == d["path"]:
            self.active = next(iter(self.docs), None)

    def read_table(self, ref, name, numbers=None):
        return [list(r) for r in self._get(ref)["tables"].get(name, [])]

    def write_table(self, ref, name, rows, numbers=None):
        if name not in self.table_kinds:
            return [TableError(-1, -1, "unknown table")]
        d = self._get(ref)
        if numbers:  # as LIRA: replace (or add) only the rows of these numbers
            kept = [r for r in d["tables"].get(name, []) if r[0] not in numbers]
            d["tables"][name] = sorted(kept + [list(r) for r in rows], key=lambda r: r[0])
        else:
            d["tables"][name] = [list(r) for r in rows]
        d["modified"] = True
        return []

    # A test sets d["selected"] / d["visible"] = {"elements": [...], "nodes": [...]};
    # without "visible" everything numbered in the tables is shown.
    def selection(self, ref, part, kind=None, check_whole=False):
        d = self._get(ref)
        tables = d["tables"]
        every = {
            "elements": [r[0] for r in tables.get("Elements_TypeAndNodes", [])],
            "nodes": [r[0] for r in tables.get("Nodes_Coordinates", [])],
        }
        if part in d:
            found = {k: list(v) for k, v in d[part].items()}
        elif part == "selected":
            found = {"elements": [], "nodes": []}
        else:
            found = every
        found = {k: v for k, v in found.items() if kind in (None, k)}
        if check_whole and part == "visible":
            found["whole"] = all(len(found[k]) == len(every[k]) for k in list(found))
        return found

    def remove_scratch_tables(self, ref):
        self._get(ref)
        return list(self.stuck_tables)

    def self_weight(self, ref, load_case, factor, elements=None):
        d = self._get(ref)
        d["self_weight"] = (load_case, factor, elements)
        d["modified"] = True
        return ""

    def script_names(self, ref):
        doc = self._get(ref) if ref else None
        return {"lira": None, "doc": doc, "adapter": self, "constants": None}

    def _press_dialog_button(self, exe, button, title=None):  # stands for Win32, not COM
        dialog = next(d for d in self.dialogs_shown if not title or title in d.title)
        if button not in dialog.buttons:
            raise LiraInputError(f"Dialog {dialog.title!r} has no button {button!r}")
        self.dialogs_shown.remove(dialog)
        return dialog

    def model_picture(self, ref, view="isometric"):
        self._get(ref)
        return 2, 1, bytes([0, 0, 255, 255] + [255] * 4)  # BGRA red, white

    def delete_self_weight(self, ref):
        self._get(ref).pop("self_weight", None)
        return ""

    def analysis_status(self):
        return {"status": self.analysis, "model": self.analysed}

    def start_analysis(self, ref):
        d = self._get(ref)
        if d["modified"]:
            raise LiraInputError(f"{d['title']} has unsaved changes; save first")
        self.analysis, self.analysed = "finished", d["path"]
        return {"status": "running", "model": d["path"]}

    def stop_analysis(self):
        self.analysis = "stopped"

    # Models whose names start with "frame" have results of 2 nodes and 3 elements.
    def results_info(self, model_path):
        if not Path(model_path).stem.startswith("frame") or model_path in self.deleted:
            return {"present": False}
        info = {"present": True, "nodes": 2, "elements": 3, "written": "2026-10-27T10:00:00"}
        warning = self.results_warning(model_path)
        return info | ({"warning": warning} if warning else {})

    def results_warning(self, model_path):
        return self.results_warnings.get(model_path)

    def delete_results(self, model_path):
        self.deleted.add(model_path)

    def punching_check(self, ref, timeout_s):
        if self._get(ref)["path"] != self.analysed:
            raise LiraInputError(f"{ref} has no analysis results: run lira_run_analysis")
        return {"model": ref, "elapsed_s": 1.0}

    def protocol(self, model_path):
        return "Расчет успешно завершен\n" if model_path == self.analysed else None

    def numbering_gaps(self, ref):
        gaps = {}
        for name in ("Nodes_Coordinates", "Elements_TypeAndNodes"):
            rows = self._get(ref)["tables"].get(name, [])
            top = max((r[0] for r in rows), default=0)
            if top != len(rows):
                gaps[name] = [len(rows), top]
        return gaps

    def pack_scheme(self, ref):
        d = self._get(ref)
        for name in ("Nodes_Coordinates", "Elements_TypeAndNodes"):
            rows = d["tables"].get(name, [])
            d["tables"][name] = [[i + 1, *r[1:]] for i, r in enumerate(rows)]
        d["modified"] = True

    def export_text(self, ref, declined=None, companions_to=None):
        d = self._get(ref)
        for file in map(Path, self.companions.get(d["title"], []) if companions_to else []):
            shutil.copyfile(file, Path(companions_to) / f"{d['title']}{file.suffix}")
        return d["text"]

    def import_file(self, path, template=""):
        text = _as_lira_imports(Path(path).read_text(encoding="utf-8-sig"))
        info = self.create_document(Path(path).stem, 5)
        self._get(info.path)["text"] = text
        self.imported_beside = sorted(p.name for p in Path(path).parent.iterdir())
        return info

    # Beam 1 with 3 sections in t*m; node 2 moving down 1.25 mm, in m.
    # Plus plate 2 with one point, stresses in tf/m2.
    def element_forces(self, model_path, elements, cases, families=None):
        if not Path(model_path).stem.startswith("frame"):
            raise LiraInputError(f"No analysis results for {Path(model_path).stem!r}")
        rows = [
            {"family": "bar", "element": 1, "case": 1, "section": s, "N": -1.0, "My": my}
            for s, my in ((1, -2.0), (2, 3.0), (3, -2.0))
        ]
        rows.append({"family": "plate", "element": 2, "case": 1, "section": 1, "Nx": 10.0})
        return [r for r in rows if not families or r["family"] in families]

    def node_displacements(self, model_path, nodes, cases):
        self.asked_nodes = nodes
        return [
            {"node": n, "case": 1, "X": 0.0, "Z": -0.00125 * (n - 1), "UY": 0.001}
            for n in nodes or [1, 2]
        ]

    # Two load combinations; one design combination per limit state of bar 1.
    def load_combinations(self, model_path):
        names = [{"number": n, "name": f"DCL{n}", "formula": 1, "special": False,
                  "seismic": False} for n in (1, 2)]  # fmt: skip
        return {"code": "SP", "combinations": names}

    def plate_reinforcement(self, model_path, elements, components, design_option=1):
        return {c: {e: v for e, v in self.plate_as.get(c, {}).items() if e in elements}
                for c in components}  # fmt: skip

    def vertical_displacements(self, model_path, nodes, case=None, combination=None):
        self.asked_vertical = (case, combination)
        return {n: self.slab_w.get(n, 0.0) for n in nodes}

    def combination_displacements(self, model_path, nodes, combinations):
        return [{"node": 2, "combination": c, "Z": -0.001 * c} for c in combinations or [1, 2]]

    def combination_forces(self, model_path, elements, combinations, families, limit_states):
        return [
            {"family": "bar", "element": 1, "combination": c, "limit_state": "ultimate",
             "section": 1, "My": -1.0 * c}
            for c in combinations or [1, 2]
        ]  # fmt: skip

    def periods(self, model_path, cases):
        modes = [{"mode": 1, "period": 0.43, "modal_mass": 44.3},
                 {"mode": 2, "period": 0.35, "modal_mass": 0.0}]  # fmt: skip
        return [{"case": 4, "type": 35, "modes": modes}]

    def reinforcement(self, model_path, elements, design_option=1):
        return [
            {"family": "bar", "element": 1, "section": 1, "symmetric": True, "AU1": 2.28,
             "PERC": 0.47},
            {"family": "plate", "element": 2, "AS1": 6.1, "AS3": 6.56,
             "errors": {"AS2": "section is not adequate"}},
        ]  # fmt: skip

    def fragment_loads(self, model_path, nodes, cases):
        return [{"node": 1, "case": 1, "RZ": 2.0, "RUY": 1.0}]

    def punching_loads(self, model_path, nodes, cases):
        return [{"node": 1, "case": 1, "N": 3.0}]

    def design_forces(self, model_path, elements, families, limit_states):
        return [
            {"family": "bar", "element": 1, "section": 1, "limit_state": "ultimate", "dcf": 1,
             "criterion": 2, "group": "A1", "load_cases": "1-3,-4", "with": "seismic",
             "N": -2.0, "My": 1.0}
        ]  # fmt: skip

    def units(self):
        g = self.units_geometry
        return Units(g, "cm", "kN", "m", "kN", "m2", "mm", "kN", "m", "kN", "m2", "C")


def _as_lira_imports(text: str) -> str:
    """As LIRA's import and export: entries 50 and 51 of document 0 left out become 1;
    entries of 0 are not written back."""
    model = TextModel.parse(text)
    body = model.body(0)
    if body is not None:
        for key in ("50", "51"):
            if f"{key};" not in body:
                body += f"\n{key}; 1 /\n"
        model.set_body(0, re.sub(r"\s*5[01]; 0 /", "", body))
    return model.render()


def fake_session(tmp_path) -> tuple[LiraSession, FakeAdapter]:
    adapter = FakeAdapter()
    worker = ComWorker(init=lambda: None, uninit=lambda: None, pump=lambda: None)
    session = LiraSession(
        Settings(log_file=tmp_path / "log.txt"),
        worker=worker,
        find=lambda: [INSTALL],
        make_adapter=lambda install: adapter,
        dialogs=lambda exe: list(adapter.dialogs_shown),
        press=adapter._press_dialog_button,
    )
    return session, adapter
