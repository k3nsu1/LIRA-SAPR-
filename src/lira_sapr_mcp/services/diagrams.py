"""Service: diagrams of forces in the bars of a frame (N, Qz, My, Mz ...) for the RPZ
appendices, from the forces LIRA gives in the design sections of each bar in one load case
or one load combination (RSN). Nothing is recalculated: the values are LIRA's, in kN and
kN·m."""

from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing import force_plots as fp
from lira_sapr_mcp.postprocessing.members import is_bar
from lira_sapr_mcp.services.rpz import KN_PER_TF
from lira_sapr_mcp.services.slabs import open_model, read_mesh

UNITS = {"N": "кН", "Qy": "кН", "Qz": "кН", "Mx": "кН·м", "My": "кН·м", "Mz": "кН·м"}
DEFAULT = ("N", "Qz", "My", "Mz")
LIMIT_STATES = {"ultimate": 1, "ultimate_long": 2, "serviceability": 3, "serviceability_long": 4}
LIMIT_NAMES = {
    "ultimate": "ПС1",
    "ultimate_long": "ПС1, длительные",
    "serviceability": "ПС2",
    "serviceability_long": "ПС2, длительные",
}
SHOWN = 20  # numbers of elements listed in a note


@dataclass
class Forces:
    path: str
    nodes: dict[int, tuple[float, float, float]]
    bars: dict[int, tuple[int, int]]
    values: dict[str, dict[int, list[float]]]  # component -> element -> per design section
    notes: list[str] = field(default_factory=list)


def _listed(numbers: list[int]) -> str:
    head = ", ".join(map(str, numbers[:SHOWN]))
    return head + (" …" if len(numbers) > SHOWN else "")


def read_forces(
    adapter: Any,
    model: str | None,
    within: list[int] | None,
    case: int | None,
    combination: int | None,
    limit_state: str,
    components: list[str],
) -> Forces:
    """On the COM thread: the bars of the model (all, or those of ``within``) and the forces
    in their design sections, converted from tf and tf·m to kN and kN·m."""
    path = open_model(adapter, model)
    mesh = read_mesh(adapter, path)
    chosen = set(within) if within is not None else None
    bars = {
        el: (ns[0], ns[1])
        for el, (fe, ns) in mesh.elements.items()
        if is_bar(fe)
        and len(ns) == 2
        and ns[0] != ns[1]
        and ns[0] in mesh.nodes
        and ns[1] in mesh.nodes
        and (chosen is None or el in chosen)
    }
    if not bars:
        raise LiraInputError("No bars (FE 1-10) among the chosen elements")
    wanted = sorted(bars)
    if combination is None:
        rows = adapter.element_forces(path, wanted, [case], ["bar"])
        what = f"Load case {case}"
    else:
        rows = adapter.combination_forces(
            path, wanted, [combination], ["bar"], [LIMIT_STATES[limit_state]]
        )
        what = f"Combination {combination}"
    sections: dict[int, dict[int, dict]] = {}
    for r in rows:
        if "shape" in r:
            raise LiraInputError(
                f"{what} is dynamic: diagrams need a static load case or a load combination"
            )
        if int(r["element"]) in bars:
            sections.setdefault(int(r["element"]), {})[int(r["section"])] = r
    values: dict[str, dict[int, list[float]]] = {c: {} for c in components}
    short = []
    for el, by_section in sections.items():
        if len(by_section) < 2 or sorted(by_section) != list(range(1, len(by_section) + 1)):
            short.append(el)
            continue
        for c in components:
            raw = [by_section[s].get(c) for s in sorted(by_section)]
            if any(v is None or not isfinite(float(v)) for v in raw):
                raise LiraInputError(
                    f"Missing or non-finite {c} in design sections of element {el}: "
                    "an unavailable result cannot be drawn as zero"
                )
            values[c][el] = [float(v) * KN_PER_TF for v in raw]
    if not sections:
        raise LiraInputError(
            f"No forces in the chosen bars for {what.lower()}: run lira_run_analysis for "
            "this version of the model"
        )
    notes = []
    missing = sorted(set(bars) - set(sections))
    if missing:
        notes.append(
            f"Нет усилий у стержней: {len(missing)} (например, {_listed(missing)}) — "
            "они показаны без эпюр."
        )
    if short:
        notes.append(
            f"Меньше двух расчётных сечений у стержней: {len(short)} ({_listed(sorted(short))}) "
            "— эпюры по ним не построены."
        )
    return Forces(path, mesh.nodes, bars, values, notes)


def summary(forces: Forces) -> dict[str, Any]:
    """Per component, the largest and the smallest value, with element and section."""
    out = {}
    for c, by_element in forces.values.items():
        hi, lo = fp.extremes(by_element)
        if hi is None or lo is None:
            continue
        out[c] = {
            "unit": "kN*m" if c.startswith("M") else "kN",
            "max": {"value": round(hi.value, 3), "element": hi.element, "section": hi.section},
            "min": {"value": round(lo.value, 3), "element": lo.element, "section": lo.section},
        }
    return out


def two_section_note(forces: Forces) -> str | None:
    """Bars with only their end sections: between them the diagram is a straight line,
    which under a load along the bar is not the diagram (the moment in a span is missed)."""
    counts = {len(v) for by_element in forces.values.values() for v in by_element.values()}
    if 2 not in counts:
        return None
    return (
        "У части стержней только два расчётных сечения (начало и конец): между ними эпюра — "
        "прямая, пролётные значения при нагрузке вдоль стержня не видны. Задайте больше "
        "расчётных сечений (таблица Elements_DesignSectionsCount) и выполните расчёт."
    )


def pictures(
    forces: Forces, out_dir: Path, stem: str, source: str, view: str
) -> tuple[list[str], list[str]]:
    """One PNG per component; returns the files and notes on bars seen end on."""
    files, notes = [], []
    for c, by_element in forces.values.items():
        path, _, end_on = fp.diagram(
            forces.nodes,
            forces.bars,
            by_element,
            c,
            UNITS[c],
            view,
            out_dir / f"{stem}_{c}_{view}.png",
            f"Эпюра {c}, {UNITS[c]}: {source}",
        )
        files.append(str(path))
        if end_on and not notes:
            notes.append(
                f"На виде {view} стержни видны с торца и без эпюр: {len(end_on)} "
                f"({_listed(end_on)}). Посмотрите их на другом виде."
            )
    return files, notes
