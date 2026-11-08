"""Loads through LIRA's text format (the COM API has no input table for loads).

Everything here runs on the COM thread.

- Reading: export the model as text and parse documents 6 and 7.
- New model: write the agent's text to a temporary file with a unique name, import it
  and save it where the agent asked.
- Changing loads of an existing model: export, replace the loads, import the text as a
  new document (with the files the export wrote beside the text: construction stages,
  densities, structural blocks), copy every input table back from the original (the
  import loses some, e.g. load case kinds), check that tables, loads and the whole text
  are as intended, and only then replace the original file. If any check fails, the
  original model stays untouched and the rebuilt one is saved next to it as a draft
  ``<name>_кор_нагр_error.lir`` with a report of the differences: whether it is of any
  use is the user's call.
"""

import logging
import math
import tempfile
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraError, LiraInputError
from lira_sapr_mcp.services.textcheck import clip, same_number, text_differences
from lira_sapr_mcp.services.textformat import Load, TextModel

log = logging.getLogger(__name__)

# Marks the rebuilt model kept when it does not match the original (owner's naming).
DRAFT_MARK = "_кор_нагр_error"


def load_dicts(loads: list[Load]) -> list[dict[str, Any]]:
    return [asdict(ld) | {"on_node": ld.on_node} for ld in loads]


# Load types of text document 6 known to load an element (probes on LIRA's examples and
# on a frame and a plate, spikes/t5_load_types.py and t5_*_types.py): 4, 6, 7, 9, 15, 16
# (self weight), 17, 26. Nodal ones are NODAL_LOAD_TYPES. LIRA dropped every other code in
# those probes and no example has one, so for another type the target's kind is unknown.
ELEMENT_LOAD_TYPES = frozenset({4, 6, 7, 9, 15, 16, 17, 26})


@dataclass(frozen=True)
class Targets:
    """Elements and nodes whose loads a call reads or replaces; the rest stay as they are."""

    elements: frozenset[int] = frozenset()
    nodes: frozenset[int] = frozenset()

    def __contains__(self, ld: Load) -> bool:
        if ld.on_node or ld.type in ELEMENT_LOAD_TYPES:
            return ld.target in (self.nodes if ld.on_node else self.elements)
        return ld.target in self.elements or ld.target in self.nodes

    def unsure(self, ld: Load) -> bool:
        """A load of a type not known to load a node or an element, whose target number
        is among these: it may belong to another object of that number."""
        return not ld.on_node and ld.type not in ELEMENT_LOAD_TYPES and ld in self


def _concerns(ld: Load, cases: list[int] | None, targets: Targets | None) -> bool:
    return (cases is None or ld.case in cases) and (targets is None or ld in targets)


def read_loads(
    adapter: Any, ref: str, cases: list[int] | None = None, targets: Targets | None = None
) -> list[dict]:
    loads = [
        ld
        for ld in TextModel.parse(adapter.export_text(ref)).loads()
        if _concerns(ld, cases, targets)
    ]
    found = load_dicts(loads)
    if targets is not None:
        for ld, d in zip(loads, found, strict=True):
            if targets.unsure(ld):
                d["target_kind_unknown"] = True
    return found


def _import_text(adapter: Any, text: str, title: str, folder: Path | None = None) -> Any:
    """Import ``text`` as model ``title`` from ``folder``, where the export put the files
    LIRA reads beside the text (``<title>.mnt``, ``<title>.ald``); a scratch folder when
    there are none."""
    if folder is None:
        with tempfile.TemporaryDirectory(prefix="lira-mcp-", ignore_cleanup_errors=True) as tmp:
            return _import_text(adapter, text, title, Path(tmp))
    src = folder / f"{title}.txt"
    src.write_text(text, encoding="utf-8-sig")
    return adapter.import_file(str(src))


def import_text_model(adapter: Any, text: str, save_as: str) -> Any:
    target = Path(save_as)
    if target.suffix.lower() != ".lir":
        raise LiraInputError("save_as must be a .lir path")
    TextModel.parse(text)  # fail early on text that is not a LIRA text model
    # A unique name, not the target's: LIRA's import takes data the text cannot hold (the
    # factors kE, at least) from its work files of an analysed model of the same name.
    info = _import_text(adapter, text, f"lira-mcp-import-{uuid.uuid4().hex[:8]}")
    ref = info.path or info.title
    try:
        return adapter.save_document(ref, str(target))
    except BaseException:
        adapter.close_document(ref, discard=True)
        raise


def _snapshot(adapter: Any, ref: str) -> dict[str, list | None]:
    tables: dict[str, list | None] = {}
    for name in sorted(adapter.table_kinds):
        try:
            tables[name] = adapter.read_table(ref, name)
        except LiraError:
            tables[name] = None  # LIRA cannot build this table for this model
    return tables


# LIRA's import merges load value rows (document 7) whose numbers differ by about 1e-4
# relatively: rows ...0.000150054 and ...0.000150069 (1.0e-4) became one, ...0.000150063
# and ...0.00015008 (1.13e-4) stayed two (the owner's model, 2026-11-02).
LOAD_REL_TOL = 2e-4


def _load_differences(a: list[Load], b: list[Load]) -> tuple[list[Load], list[Load]]:
    """Loads of ``a`` with no equal in ``b``, and of ``b`` with none in ``a``; values are
    equal up to LIRA's six significant digits (10/3 comes back as 3.33333) and the rows
    its import merges (``LOAD_REL_TOL``)."""

    def values(ld: Load) -> list[float]:
        v = list(ld.values)
        while v and v[-1] == 0:  # LIRA pads value rows with zeros on export
            v.pop()
        return v

    def close(x: float, y: float) -> bool:
        return same_number(x, y) or math.isclose(x, y, rel_tol=LOAD_REL_TOL)

    def same(p: list[float], q: list[float]) -> bool:
        return len(p) == len(q) and all(map(close, p, q))

    # Each load is matched with any one equal to it among the loads of its case, target,
    # type and direction (a model has a million loads): sorting by values would pair
    # 3.333333 and 3.33333 wrongly when another load of the same element lies between them.
    left: dict[tuple, list[tuple[Load, list[float]]]] = defaultdict(list)
    for ld in b:
        left[(ld.case, ld.target, ld.type, ld.direction)].append((ld, values(ld)))
    extra = []
    for ld in a:
        group = left[(ld.case, ld.target, ld.type, ld.direction)]
        v = values(ld)
        match = next((i for i, (_, w) in enumerate(group) if same(v, w)), None)
        if match is None:
            extra.append(ld)
        else:
            group.pop(match)
    return extra, [ld for group in left.values() for ld, _ in group]


def _same_loads(a: list[Load], b: list[Load]) -> bool:
    return _load_differences(a, b) == ([], [])


def _loads_problem(got: list[Load], wanted: list[Load]) -> str | None:
    """The user's report line on loads of the rebuilt model that differ from the request."""
    extra, missing = _load_differences(got, wanted)
    if not extra and not missing:
        return None

    def shown(loads: list[Load]) -> str:
        return "; ".join(
            f"загружение {ld.case}, {'узел' if ld.on_node else 'элемент'} {ld.target}, "
            f"тип {ld.type}, направление {ld.direction}, значения "
            f"{' '.join(f'{v:g}' for v in ld.values)}"
            for ld in loads[:5]
        )

    parts = ["нагрузки пересобранной модели отличаются от запрошенных"]
    if missing:
        parts.append(f"нет {len(missing)} запрошенных (например: {shown(missing)})")
    if extra:
        parts.append(f"есть {len(extra)} лишних (например: {shown(extra)})")
    return "; ".join(parts)


def _changed_kinds(before: list, now: list) -> list[str]:
    """Load case kinds (column 4 of LoadCases) that differ after the rebuild, as 'old -> new'.

    The text keeps kinds as SNiP-style codes and the import reads them in LIRA's default
    combination norms (SP 20.13330), so every rebuild scrambles them. Writing LoadCases
    back restores them only when the model uses those norms: LIRA maps the written names
    into the dictionary of the default norms. Rows without a kind on either side are left to
    the full check.
    """
    old = {row[0]: row[3] for row in before if len(row) > 3}
    return [
        f"{row[0]}: {old[row[0]]} -> {row[3]}"
        for row in now
        if len(row) > 3 and row[3] is not None and old.get(row[0]) not in (None, row[3])
    ]


# Entries of document 0 that LIRA's import sets to 1 when a text leaves them out, while
# its export leaves them out when they are 0 (the help does not name them).
IMPORT_ONES = ("50", "51")


def _keep_zero_entries(model: TextModel) -> None:
    """Write entries 50 and 51 of document 0 as 0 when the text leaves them out, so the
    rebuilt model keeps the original's values."""
    body = model.body(0)
    if body is None:
        return
    keys = {e.split(";")[0].strip() for e in body.split("/") if ";" in e}
    missing = [k for k in IMPORT_ONES if k not in keys]
    if missing:
        model.set_body(0, body.rstrip() + "\n" + "".join(f"{k}; 0 /\n" for k in missing))


def _same(a: Any, b: Any) -> bool:
    """Equal table contents, numbers up to rounding: a coordinate 4.050000000000001
    comes back from the text as 4.05, and LIRA keeps some values in single precision
    (about 7 digits): a bed coefficient 145.84943 written back reads 145.84944. Integers
    (node and element numbers) are equal only when they are."""
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, float | int) and isinstance(b, float | int):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _restore_and_check(
    adapter: Any,
    new_ref: str,
    before: dict[str, list | None],
    wanted: list[Load],
    expected_text: str,
) -> tuple[list[str], list[str]]:
    """Copy the original's input tables into the rebuilt model; return the tables
    written without errors and every way the rebuilt model still differs from the original (tables,
    loads, and its whole text against ``expected_text``), in Russian for the user's
    report."""
    restored, failed = [], {}
    # Load cases first: once construction stages are back, LIRA refuses LoadCases.
    for name in sorted(before, key=lambda n: n != "LoadCases"):
        rows = before[name]
        if rows is None:
            continue
        try:
            now = adapter.read_table(new_ref, name)
        except LiraError:
            now = None
        if not _same(now, rows):
            try:
                errors = [e.message for e in adapter.write_table(new_ref, name, rows)]
            except LiraError as exc:  # LIRA refuses the whole table, e.g. LoadCases
                errors = [str(exc)]  # of a model with construction stages
            if errors:
                failed[name] = errors[:5]
            else:
                restored.append(name)
    after = _snapshot(adapter, new_ref)
    problems = []
    # A table LIRA cannot build or refused to take is left to the table check below.
    if "LoadCases" not in failed and None not in (before.get("LoadCases"), after.get("LoadCases")):
        kinds = _changed_kinds(before["LoadCases"], after["LoadCases"])
        if kinds:
            problems.append(
                "изменились виды загружений: нормы сочетаний модели не совпадают с нормами "
                "ЛИРЫ по умолчанию (СП 20.13330), которые получает модель, пересобранная из "
                f"текста, а API ЛИРЫ 2024 R2 другие нормы сохранить не позволяет "
                f"({'; '.join(kinds)})"
            )
    for name, errors in failed.items():
        problems.append(f"таблицу {name} не удалось восстановить: {'; '.join(errors)}")
    for name, rows in before.items():
        if rows is not None and not _same(after.get(name), rows):
            problems.append(_table_difference(name, rows, after.get(name)))
    declined: list[str] = []
    try:
        text = adapter.export_text(new_ref, declined=declined)
        got = TextModel.parse(text).loads()
    except LiraError as exc:
        problems.append(
            f"пересобранную модель не удалось выгрузить в текст, чтобы сверить нагрузки: {exc}"
        )
    else:
        loads_problem = _loads_problem(got, wanted)
        if loads_problem:
            problems.append(loads_problem)
        problems.extend(text_differences(expected_text, text))
    for question in declined:
        problems.append(
            f"при выгрузке пересобранной модели в текст ЛИРА спросила {question}; сервер "
            "ответил «Нет» — это оставляет модель как есть"
        )
    return restored, problems


def _table_difference(name: str, before: list, after: list | None, shown: int = 10) -> str:
    """How a table of the rebuilt model differs from the original, row by row."""
    if after is None:
        return f"таблицу {name} ЛИРА не может построить в пересобранной модели"
    old = {i: row for i, row in enumerate(before)}
    new = {i: row for i, row in enumerate(after)}
    lines = [
        f"строка {i + 1}: {_shown_row(old.get(i))} -> {_shown_row(new.get(i))}"
        for i in sorted(old.keys() | new.keys())
        if old.get(i) != new.get(i)
    ]
    more = f"; отличаются ещё строк: {len(lines) - shown}" if len(lines) > shown else ""
    return f"таблица {name} отличается: {'; '.join(lines[:shown])}{more}"


def _shown_row(row: list | None, limit: int = 200) -> str:
    """A row without its trailing empty cells (CollectionOfMasses has 1001), clipped."""
    if row is None:
        return "нет строки"
    cells = list(row)
    while cells and cells[-1] is None:
        cells.pop()
    text = repr(cells)
    return text if len(text) <= limit else text[:limit] + " ...]"


def draft_path(original: Path) -> Path:
    """``<name>_кор_нагр_error.lir`` next to the original, numbered if taken."""
    base = f"{original.stem}{DRAFT_MARK}"
    path, n = original.with_name(base + original.suffix), 1
    while path.exists() or path.with_suffix(".txt").exists():
        n += 1
        path = original.with_name(f"{base}_{n}{original.suffix}")
    return path


def _save_draft(adapter: Any, original: str, new_ref: str, problems: list[str]) -> LiraError:
    """Keep the rebuilt model as a draft next to the original, with a report; the
    original is untouched. Returns the error to raise."""
    draft = draft_path(Path(original))
    report = draft.with_suffix(".txt")
    try:
        adapter.save_document(new_ref, str(draft))
        new_ref, kept = str(draft), f"was saved as a draft {draft}"
        kept_ru = f"сохранена как черновик {draft}"
    except LiraError as exc:
        kept = f"could not be saved as a draft ({exc})"
        kept_ru = f"не сохранена: ЛИРА не смогла записать черновик ({exc})"
    try:
        adapter.close_document(new_ref, discard=True)
    except LiraError:
        log.warning("could not close the rebuilt model %s", new_ref, exc_info=True)
    lines = "\n".join(f"- {p}" for p in problems)
    try:
        report.write_text(
            f"Нагрузки в модели {original} не изменены: модель, пересобранная с новыми "
            f"нагрузками, не совпала с ней.\nИсходная модель не изменена. Пересобранная "
            f"модель {kept_ru}.\nНужен ли черновик, решаете вы. Прежде чем его "
            f"рассчитывать, исправьте расхождения ниже; ненужный черновик удалите.\n\n"
            f"Расхождения:\n{lines}\n",
            encoding="utf-8",
        )
        written = f"report {report}"
    except OSError as exc:
        written = f"the report could not be written ({exc})"
    log.warning("loads of %s not changed; rebuilt model %s; %s", original, kept, written)
    return LiraError(
        f"Loads were not changed: the rebuilt model did not match the original, and the "
        f"original model is untouched. The rebuilt model with the new loads {kept}; "
        f"{written}. Tell the user: whether the draft is of use is their call, and it "
        f"must not be analysed before the differences are fixed. Differences (in "
        f"Russian, as in the report): "
        f"{clip('; '.join(problems), 3000, ' ... (the report has all)')}"
    )


def _reopen_original(adapter: Any, path: str, new_ref: str, exc: LiraError) -> None:
    """The rebuilt model could not be saved over the original: drop it and open the
    original file again, as it was before the change."""
    try:
        adapter.close_document(new_ref, discard=True)
    except LiraError:
        log.warning("could not close the rebuilt model %s", new_ref, exc_info=True)
    try:
        adapter.open_document(path)
        state = "the original model is open again"
    except LiraError as reopen:
        state = (
            f"the original model could not be opened again ({reopen}): restore it with "
            "lira_restore_backup"
        )
    raise LiraError(f"Loads were not changed: LIRA could not save {path} ({exc}); {state}")


NUMBERED = {"Nodes_Coordinates": "nodes", "Elements_TypeAndNodes": "elements"}


def _pack_first(adapter: Any, path: str, pack: bool) -> dict[str, Any] | None:
    """An unpacked scheme would come back from the text with its deleted nodes and
    elements alive: refuse it, or pack and save it when the user agreed. Returns what
    was packed, or None for a packed scheme."""
    gaps = adapter.numbering_gaps(path)
    if not gaps:
        return None
    shown = "; ".join(
        f"{rows} {NUMBERED.get(name, name)} numbered up to {top}"
        for name, (rows, top) in gaps.items()
    )
    if not pack:
        raise LiraInputError(
            f"The scheme of {path} is not packed ({shown}): deleted nodes and elements keep "
            "their numbers, and LIRA's text format, through which loads are changed, would "
            "bring them back. Ask the user whether to pack the scheme: packing removes "
            "deleted nodes and elements and numbers the rest without gaps, so node and "
            "element numbers change; nothing else is changed. If the user agrees, call "
            "again with pack=true: the server packs and saves the model and changes no "
            "loads; then read the loads again and send them in the new numbering. "
            "Otherwise the loads cannot be changed this way."
        )
    adapter.pack_scheme(path)
    adapter.save_document(path)
    log.info("packed and saved %s before changing loads (%s)", path, shown)
    return {
        NUMBERED.get(name, name): {"count": rows, "highest_number_before": top}
        for name, (rows, top) in gaps.items()
    }


def set_loads(
    adapter: Any,
    ref: str,
    loads: list[Load],
    cases: list[int] | None,
    pack: bool = False,
    targets: Targets | None = None,
) -> dict[str, Any]:
    """Replace the loads of ``cases`` (all cases when None) in a saved model; with
    ``targets``, only the loads on those elements and nodes. With ``pack``, an unpacked
    scheme is only packed and saved: the loads were given in the old numbering, so they
    are not applied."""
    orig = adapter.document_info(ref)
    if not orig.saved:
        raise LiraInputError("Save the model to a .lir file before changing its loads")
    if orig.modified:
        raise LiraInputError(f"{orig.title} has unsaved changes: save it first")
    if cases is not None:
        stray = sorted({ld.case for ld in loads} - set(cases))
        if stray:
            raise LiraInputError(f"Loads for load cases {stray} are outside load_cases={cases}")
    if targets is not None:
        outside = sorted({ld.target for ld in loads if ld not in targets})
        if outside:
            raise LiraInputError(
                f"Loads on {outside[:20]} are outside the elements and nodes given: only "
                "loads on those are replaced"
            )
    packed = _pack_first(adapter, orig.path, pack)
    if packed:
        return {
            "model": orig.path,
            "packed": packed,
            "loads_changed": False,
            "next": (
                "The scheme is packed and saved; node and element numbers changed, so the "
                "loads sent with this call (in the old numbering) were not applied. Read "
                "the model again (lira_read_loads, lira_read_table) and call lira_set_loads "
                "with targets in the new numbering."
            ),
        }
    with tempfile.TemporaryDirectory(prefix="lira-mcp-", ignore_cleanup_errors=True) as tmp:
        # The import finds the stages and blocks of the original beside the text.
        model = TextModel.parse(adapter.export_text(orig.path, companions_to=tmp))
        if targets is not None:
            unsure = sorted(
                {
                    ld.type
                    for ld in model.loads()
                    if _concerns(ld, cases, targets) and targets.unsure(ld)
                }
            )
            if unsure:
                raise LiraInputError(
                    f"Load types {unsure} on these numbers: the server does not know whether "
                    "such a load is on a node or on an element, so it cannot tell which to "
                    "replace. Replace the loads of the whole load case instead (without "
                    "elements and nodes)"
                )
        kept = [ld for ld in model.loads() if not _concerns(ld, cases, targets)]
        wanted = kept + loads
        model.set_loads(wanted)
        before = _snapshot(adapter, orig.path)
        expected = model.render()
        _keep_zero_entries(model)
        new = _import_text(adapter, model.render(), orig.title, Path(tmp))
    new_ref = new.path or new.title
    try:
        restored, problems = _restore_and_check(adapter, new_ref, before, wanted, expected)
        if not problems:
            adapter.close_document(orig.path, discard=True)
    except BaseException:
        try:
            adapter.close_document(new_ref, discard=True)
        except LiraError:
            log.warning("could not close the rebuilt model %s", new_ref, exc_info=True)
        raise
    if problems:
        raise _save_draft(adapter, orig.path, new_ref, problems)
    try:
        adapter.save_document(new_ref, orig.path)
    except LiraError as exc:
        _reopen_original(adapter, orig.path, new_ref, exc)
    log.info("loads of %s replaced; tables restored: %s", orig.path, restored)
    return {
        "model": orig.path,
        "loads_now": len(wanted),
        "load_cases_changed": cases if cases is not None else "all",
        "tables_restored_after_import": restored,
        "checked": (
            "all input tables and the whole text equal the original; loads equal the request"
        ),
    }
