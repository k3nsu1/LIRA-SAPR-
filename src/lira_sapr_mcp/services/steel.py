"""Service: utilization percentages of steel sections from LIRA's steel check (LIR-STK),
read from the .rez file the analysis writes, as facts: the numbers are LIRA's, copied as
written; the only verdict is the fact that a percentage is above 100."""

from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing import steel

MEASURES = {"1LS": "1ПС (прочность и устойчивость)", "2LS": "2ПС (прогибы, гибкость)",
            "LB": "местная устойчивость"}  # fmt: skip
SORTS = ("max", *steel.MEASURES)
LIMIT = 100.0  # percent: above it LIRA's check is not met


def rez_name(title: str, design_option: int) -> str:
    """LIRA names the file of design option 1 ``<title>.rez``, of option n
    ``<title>#<n-1>.rez`` (checked on a copy of example 04: VarSetName "Вариант 2" in
    ``#1``)."""
    if design_option < 1:
        raise LiraInputError("design_option: 1 or more")
    return f"{title}{'' if design_option == 1 else f'#{design_option - 1}'}.rez"


def find_rez(work: str | None, model_path: str, design_option: int) -> tuple[Path, str | None]:
    """The .rez of a model in LIRA's work folder, and a warning when it is older than the
    model file (the model changed after the steel check)."""
    if not work:
        raise LiraInputError("LIRA's work folder is unknown: give rez_path")
    title = Path(model_path).stem
    path = Path(work) / rez_name(title, design_option)
    if not path.is_file():
        found = sorted(p.name for p in Path(work).glob(f"{title}*.rez"))
        raise LiraInputError(
            f"No {path.name} in LIRA's work folder {work}: run lira_run_analysis with the "
            "steel check (design option with steel sections)"
            + (f"; found: {', '.join(found)}" if found else "")
        )
    model = Path(model_path)
    warning = None
    if model.is_file() and model.stat().st_mtime > path.stat().st_mtime:
        warning = (
            f"{path.name} is older than {model.name}: the model was saved after the steel "
            "check; rerun the analysis"
        )
    return path, warning


def origin_warning(file: steel.RezFile, model_path: str | None) -> str | None:
    """A warning when the model title the .rez names is not the model's file name: LIRA's
    work folder is shared, and a model of the same name elsewhere writes the same file."""
    title = (file.general.get("Title") or "").strip()
    if not model_path or not title:
        return None
    stem = Path(model_path).stem
    if title.casefold() == stem.casefold():
        return None
    return (
        f"The .rez names the model {title!r}, not {stem!r}: it may be another model's steel "
        "check; rerun the analysis of this model"
    )


def _value(text: str) -> float | None:
    """A percentage as a number for sorting and the 100 % fact; None where LIRA has no
    result (NR, the service value 1e20), as LiraRez does."""
    if text == "NR" or not text.isascii():
        return None
    value = float(text)
    return None if value >= steel.SERVICE_VALUE else value


def _section_of(file: steel.RezFile, element: steel.RezElement) -> dict[str, Any]:
    s = file.assigned[element.asn]
    return {"section": s.number, "section_name": s.title or s.name, "stiffness": s.stiffness}


def check(
    file: steel.RezFile,
    elements: list[int] | None = None,
    stiffnesses: list[int] | None = None,
    sort_by: str = "max",
    top: int | None = 20,
) -> dict[str, Any]:
    """Rows per element (assigned sections, LIRA's strings), groups with their maxima
    (LiraRez's rule: the largest value, ties to the smaller element) and the elements over
    100 %. ``elements`` and ``stiffnesses`` narrow the elements (their union when both)."""
    if sort_by not in SORTS:
        raise LiraInputError(f"sort_by: one of {', '.join(SORTS)}")
    warnings: list[str] = []
    if elements is None and stiffnesses is None:
        chosen = list(file.elements)
    else:
        group = steel.summarize(file, stiffnesses, elements)
        warnings += [w for w in group.warnings if w != "группа не содержит ни одного элемента"]
        chosen = group.elements
    if not chosen:
        raise LiraInputError("No elements of the .rez match elements / stiffnesses")
    rows, over, blind = [], [], []
    for number in chosen:
        e = file.elements[number]
        row: dict[str, Any] = {"element": number} | _section_of(file, e)
        above, missing = [], []
        for m, text in zip(steel.MEASURES, e.asn_pct, strict=True):
            row[m] = text
            value = _value(text)
            if value is None:
                missing.append(m)
            elif value > LIMIT:
                above.append(m)
        row["over_100"] = above
        if above:
            over.append({"element": number, "measures": above} | _section_of(file, e))
        if missing:
            blind.append({"element": number, "measures": missing})
        rows.append(row)

    def key(row: dict) -> tuple[bool, float]:
        names = steel.MEASURES if sort_by == "max" else (sort_by,)
        values = [v for m in names if (v := _value(row[m])) is not None]
        return (not values, -max(values) if values else 0.0)

    rows.sort(key=key)
    groups = _groups(file, chosen)
    shown = rows if top is None else rows[: max(1, top)]
    general = ("Title", "VarSetName", "BuildingCodes", "ForcesSrc")
    out: dict[str, Any] = {
        "general": {k: file.general[k] for k in general if k in file.general},
        "measures": MEASURES,
        "elements_checked": len(rows),
        "over_100_count": len(over),
        "over_100": over,
        # No result is not "not above 100 %": the agent must not read 0 over as all passed
        "no_result_count": len(blind),
        "no_result": blind,
        "groups": groups,
        "rows": shown,
        "rows_shown": len(shown),
    }
    if warnings:
        out["warnings"] = warnings
    return out | {"_all_rows": rows}


def _groups(file: steel.RezFile, chosen: list[int]) -> list[dict[str, Any]]:
    """Per stiffness (from the assigned section's name, as LIRA+ groups), else per assigned
    section: the maximum of each measure and the element where it is."""
    by: dict[tuple[str, int], list[int]] = {}
    for number in chosen:
        s = file.assigned[file.elements[number].asn]
        place = ("stiffness", s.stiffness) if s.stiffness is not None else ("section", s.number)
        by.setdefault(place, []).append(number)
    out = []
    for (kind, number), members in sorted(by.items()):
        g = steel.summarize(file, None, members)
        names = sorted({_section_of(file, file.elements[n])["section_name"] or "" for n in members})
        row: dict[str, Any] = {kind: number, "sections": names, "elements": len(members)}
        for m, best in zip(steel.MEASURES, (g.uls, g.sls, g.local), strict=True):
            row[m] = {"max": best.text, "element": best.element or None,
                      "no_result": best.no_result}  # fmt: skip
        out.append(row)
    return out
