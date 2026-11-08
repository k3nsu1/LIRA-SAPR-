"""Slabs of a LIRA model for the post-processing package.

The reading runs on the COM thread: input tables give the mesh (converted to meters),
the results library gives Z displacements of the slab nodes. The computing runs in the
tools' thread on plain arrays (``lira_sapr_mcp.postprocessing``).
"""

import re
import shutil
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from lira_sapr_mcp.adapters import word
from lira_sapr_mcp.adapters.base import bad_model_name
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import format_numbers, parse_numbers
from lira_sapr_mcp.postprocessing import plots
from lira_sapr_mcp.postprocessing.deflection import (
    Deflections,
    panel_lows,
    relative_deflections,
    worst_spans,
)
from lira_sapr_mcp.postprocessing.dxf import field_plan, write_zones
from lira_sapr_mcp.postprocessing.mesh import LEVEL_TOLERANCE, Slab, find_slabs, outline_loops
from lira_sapr_mcp.postprocessing.report import Report, SlabPart, to_docx, to_pdf
from lira_sapr_mcp.postprocessing.settlement import (
    WITHIN_A_WALL_FROM,
    End,
    relative_differences,
    zones_over,
)

TO_METERS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}
ALL_DOFS = "все степени свободы"  # kind of a rigid body (PRBs) that ties Z too
MM = 1000.0  # millimetres in a metre
SPANS_SHOWN = 3
LISTED_TO_AGENT = 10  # places of one kind in the answer; the report has them all
LOWS_TO_AGENT = 5
NO_LIMIT = "no limit set: facts only"  # the project gave no limit (SP RK EN 1993-1-1 7.2.1)
HOW_DEFLECTION = (
    "Прогиб оценивается по линиям между опорами, как по линии взгляда: берутся все пары точек "
    "опор, линия между которыми не выходит за край плиты и отверстия, не проходит через другую "
    "опору (ближе 100 мм) и не пересекает стену. Прогиб в точке линии — перемещение плиты в этой "
    "точке минус перемещение прямой между концами линии, знак как в ЛИРЕ: минус — плита ниже "
    "прямой; пролёт — длина линии. Свободный край, к "
    "которому такие линии не подходят, — консоль: вылет измеряется по кратчайшему расстоянию от "
    "края до опоры, прогиб смотрится на конце этой линии, относительно опоры, пролёт равен "
    "двойному вылету. Положительный прогиб — плита выше прямой (выгиб). С предельным, если он "
    "задан "
    "для проекта, сравнивается абсолютная величина прогиба."
)
ON_SPANS = (
    f"На изополях номерами отмечены {SPANS_SHOWN} самых опасных участка плиты и все участки "
    "с превышением предела, числа по номерам — в тексте: линия, по которой оценён прогиб, и её "
    "худшая точка. Изополя считаются во всех узлах одинаково: перемещение узла минус перемещение "
    "поверхности через опоры; между опорами это плоскость через три из них, за крайними опорами — "
    "прямая между двумя ближайшими. Если все опоры в нуле, изополя равны перемещениям ЛИРЫ. Числа "
    "участков относятся к их линиям, поэтому в этой точке могут отличаться от цвета изополя, когда "
    "опоры смещаются по-разному. Чёрными точками подписаны "
    "наинизшие точки панелей: перемещение в них, мм; на линию они обычно не попадают, поэтому "
    "показаны без оценки. Шкала прогиба своя на каждой плите."
)
PILE = 57  # FE type, of the last two digits: 257 is its non-linear analogue
ON_FOUNDATION = (
    "Плита, которую держат в основном одноузловые упругие опоры — под всей плитой или под "
    "отдельными точками (сваи), — или у большинства элементов которой заданы коэффициенты постели "
    "C1, C2, считается по осадке: пролётов между опорами "
    "у неё нет, прогиб относительно опор не применяется. Знак как в ЛИРЕ: минус — "
    "вниз; с пределом (СП РК 5.01-102-2013, прил. В, или заданным для проекта) сравнивается "
    "абсолютная величина, поэтому подъём сверх предела — тоже превышение. Показана осадка и "
    "относительная разность осадок Δs/L = |s1 − s2| / L между вертикальными "
    "конструкциями, опирающимися на плиту: наибольшая, а при превышении предела — все участки с "
    "превышением. Колонна — одна точка: на АЖТ это его центр со средней осадкой узлов, потому что "
    "АЖТ поворачивается как целое и разница его узлов — наклон, а не разность осадок. Стена или "
    "пилон — одна конструкция: между разными конструкциями сравниваются все пары точек, внутри "
    "одной — только далеко отстоящие. Зоны, где осадка больше предельной, обведены красным."
)


# For the report: the answer to the agent carries the server's own English warning.
STALE_RESULTS = (
    "Модель сохранена после расчёта: если она менялась, результаты устарели и расчёт нужно "
    "повторить."
)


@dataclass(frozen=True)
class Note:
    """Something the agent and the engineer should know about a reading."""

    text: str
    ru: str


@dataclass
class Mesh:
    """The model's mesh in meters, as the post-processing package takes it. ``elements`` leaves
    out one-node elements that hold nothing along Z."""

    nodes: dict[int, tuple[float, float, float]]
    elements: dict[int, tuple[int, tuple[int, ...]]]
    restrained: set[int]
    groups: list[set[int]]
    notes: list[Note] = field(default_factory=list)
    bedded: set[int] = field(default_factory=set)  # plates with bedding coefficients


def open_model(adapter: Any, model: str | None) -> str:
    """Path of a saved model, opened in LIRA if it is a .lir path that is not open yet
    (LIRA closes a model after its analysis): slabs need its tables and its results."""
    if model and model.lower().endswith(".lir"):
        try:
            info = adapter.document_info(model)
        except LiraInputError:
            info, _ = adapter.open_document(model)
    else:
        info = adapter.document_info(model)
    if not info.saved:
        raise LiraInputError(
            f"{info.title} is not saved: save it and run lira_run_analysis before reading slabs"
        )
    return info.path


def _parameter(name: str, parameters: str) -> float | None:
    found = re.search(rf"\b{name}:(\S+)", parameters)
    try:
        return float(found[1]) if found else None
    except ValueError:
        return None


def _holds_along_z(parameters: Any) -> bool:
    """Whether a stiffness holds along Z: FE 56 by ``"Rx:200 Ry:200 Rz:0 ..."``, FE 51 by its one
    direction and stiffness, ``"EF:1000 n:3 ..."`` (3 is Z). True when they do not say."""
    text = str(parameters or "")
    rz = _parameter("Rz", text)
    if rz is not None:
        return rz != 0
    direction = _parameter("n", text)
    if direction is not None:
        return direction == 3 and _parameter("EF", text) != 0
    return True


def _no_vertical_stiffness(adapter: Any, ref: str, elements: dict) -> set[int]:
    """One-node elements whose stiffness says they hold nothing along Z. A pile (FE 57) works
    along its axis and has no such record, so a model of piles alone reads nothing more. An
    element whose stiffness does not say stays a support."""
    asked = {el for el, (fe, ns) in elements.items() if len(ns) == 1 and fe % 100 != PILE}
    if not asked:
        return set()
    free = {
        int(r[0])
        for r in adapter.read_table(ref, "Stiffnesses")
        if len(r) > 4 and r[0] is not None and not _holds_along_z(r[4])
    }
    if not free:
        return set()
    return {
        int(r[0])
        for r in adapter.read_table(ref, "Elements_Stiffnesses")
        if len(r) > 1 and r[0] is not None and r[1] is not None
        and int(r[0]) in asked and int(r[1]) in free
    }  # fmt: skip


def read_mesh(adapter: Any, ref: str) -> Mesh:
    """Nodes, elements, Z restraints and rigid / coupled node groups of an open model."""
    scale = TO_METERS.get(adapter.units().geometry)
    if scale is None:
        raise LiraInputError(f"Unknown geometry unit {adapter.units().geometry!r} in LIRA")
    nodes = {
        int(r[0]): (float(r[1]) * scale, float(r[2]) * scale, float(r[3]) * scale)
        for r in adapter.read_table(ref, "Nodes_Coordinates")
        if len(r) >= 4 and None not in r[:4]
    }
    elements = {}
    for r in adapter.read_table(ref, "Elements_TypeAndNodes"):
        if len(r) >= 3 and None not in r[:3]:
            elements[int(r[0])] = (int(r[1]), tuple(_numbers(r[2])))
    restrained = {
        int(r[0])
        for r in adapter.read_table(ref, "Nodes_Restraints")
        if len(r) > 3 and r[0] is not None and _flag(r[3])
    }
    groups, notes = [], []
    skipped = 0
    for r in adapter.read_table(ref, "PRBs"):
        if len(r) < 4 or r[2] is None:
            continue
        if str(r[1] or "").strip().casefold() != ALL_DOFS:
            skipped += 1
            continue
        groups.append({int(r[2]), *(parse_numbers(str(r[3] or "")) or [])})
    if skipped:
        notes.append(
            Note(
                f"{skipped} rigid bodies that do not tie all degrees of freedom are not taken "
                "as supports",
                f"Абсолютно жёстких тел, связывающих не все степени свободы, не считаются "
                f"опорами: {skipped}. Плиты, опёртые через них, могут быть без опор.",
            )
        )
    for r in adapter.read_table(ref, "CoupledDOFs"):
        if len(r) > 9 and _flag(r[4]):
            groups.append(set(parse_numbers(str(r[9] or "")) or []))
    free = _no_vertical_stiffness(adapter, ref, elements)
    if free:
        for el in free:
            del elements[el]
        notes.append(
            Note(
                f"{len(free)} one-node elements in the model have no stiffness along Z and are "
                "not taken as supports",
                f"Одноузловых элементов в модели без жёсткости по Z, которые не считаются опорами: "
                f"{len(free)}.",
            )
        )
    bedded = {
        int(r[0])
        for r in adapter.read_table(ref, "Plates_C1C2")
        if len(r) > 2 and r[0] is not None and (float(r[1] or 0) > 0 or float(r[2] or 0) > 0)
    }
    return Mesh(nodes, elements, restrained, groups, notes, bedded)


def slabs_of(mesh: Mesh, within: list[int] | None, tolerance: float) -> list[Slab]:
    slabs = find_slabs(
        mesh.nodes,
        mesh.elements,
        mesh.restrained,
        mesh.groups,
        set(within) if within else None,
        tolerance,
        mesh.bedded,
    )
    if not slabs:
        where = "among the given elements" if within else "in the model"
        raise LiraInputError(
            f"No slabs {where}: a slab is a set of bending plates or shells whose nodes "
            f"lie on one level (within {tolerance * 1000:g} mm)"
        )
    return slabs


def _part_note(slab: Slab) -> Note:
    return Note(
        f"Slab {slab.number}: only {len(slab.elements)} of its {slab.whole_elements} plates were "
        "given, and they are measured as a slab of their own: a line through the rest is not "
        "read and their edge counts as a free one, so a cantilever there may be false. Give all "
        "of its plates for the deflection of the slab.",
        f"Плита {slab.number}: выбрано {len(slab.elements)} из {slab.whole_elements} её элементов, "
        "и они посчитаны как отдельная плита: линии через остальную часть не оцениваются, а край "
        "выбора считается свободным краем, поэтому консоль у него может быть ложной. Прогиб плиты "
        "в целом — при выборе всех её элементов.",
    )


def files_name(stem: str, slabs: list[Slab], chosen: bool) -> str:
    """The start of the report's name: a report on chosen slabs says which, so that it does not
    replace the one on the whole model, which has no numbers."""
    if not chosen:
        return f"{stem}_slab"
    numbers = [s.number for s in slabs]
    if len(numbers) <= 4:
        which = "_".join(map(str, numbers))
    else:
        which = f"{numbers[0]}-{numbers[-1]}"
        if numbers[-1] - numbers[0] + 1 != len(numbers):  # 1,3,5,7,9 must not replace 1 to 9
            which += f"_{zlib.crc32(','.join(map(str, numbers)).encode()):08x}"
    return f"{stem}_slab{which}" + ("_part" if any(s.part for s in slabs) else "")


def slab_file(slab: Slab) -> str:
    """A slab in the names of its own files."""
    return f"slab{slab.number}" + ("_part" if slab.part else "")


def slab_title(number: int, z: float, part: bool) -> str:
    return f"Плита {number}{' (часть)' if part else ''}, отм. {level_text(z)}"


def part_fields(slab: Slab) -> dict[str, Any]:
    """For the answers and the reports, when only some plates of a slab were asked for."""
    return {"part_of_slab": True, "whole_slab_elements": slab.whole_elements} if slab.part else {}


def part_line(s: dict[str, Any], count: int) -> list[str]:
    if not s.get("part_of_slab"):
        return []
    return [f"Выбрана часть плиты: {count} из {s['whole_slab_elements']} элементов."]


def read_slabs(
    adapter: Any,
    model: str | None,
    within: list[int] | None,
    case: int | None,
    combination: int | None,
    tolerance: float = LEVEL_TOLERANCE,
) -> tuple[str, list[Slab], list[np.ndarray], list[Note]]:
    """On the COM thread: the model's path, its slabs, the Z displacement (m) of each
    slab's nodes, and notes about what was left out."""
    path = open_model(adapter, model)
    mesh = read_mesh(adapter, path)
    slabs = slabs_of(mesh, within, tolerance)
    mesh.notes += [_part_note(s) for s in slabs if s.part]
    wanted = sorted({int(n) for s in slabs for n in s.nodes})
    w = adapter.vertical_displacements(path, wanted, case, combination)
    missing = [n for n in wanted if n not in w]
    if missing:
        raise LiraInputError(
            f"No displacements for {len(missing)} slab nodes (e.g. {missing[:5]}): run "
            "lira_run_analysis for this version of the model"
        )
    return path, slabs, [np.array([w[int(n)] for n in s.nodes]) for s in slabs], mesh.notes


def deflection_summaries(
    slabs: list[Slab],
    ws: list[np.ndarray],
    found: list[Deflections],
    settlement_limit: float | None = None,
    relative_limit: float | None = None,
    within_a_wall_from: float = WITHIN_A_WALL_FROM,
    fraction: float | None = None,
) -> list[dict[str, Any]]:
    return [
        deflection_summary(s, w, d, settlement_limit, relative_limit, within_a_wall_from, fraction)
        for s, w, d in zip(slabs, ws, found, strict=True)
    ]


def deflection_summary(
    slab: Slab,
    w: np.ndarray,
    d: Deflections,
    settlement_limit: float | None = None,
    relative_limit: float | None = None,
    within_a_wall_from: float = WITHIN_A_WALL_FROM,
    fraction: float | None = None,
) -> dict[str, Any]:
    """What is known of a slab (the report takes all of it, the agent ``for_agent``): the worst
    line by the ratio to the allowed deflection (mm, m), the lines of the next most dangerous
    places, the low point of each panel and the displacement largest in size, in LIRA's sign. A
    slab on one-node elastic supports has no spans: its settlement and relative settlement
    difference."""
    low = int(np.argmax(np.abs(w)))
    out: dict[str, Any] = {
        "slab": slab.number,
        "z_m": round(slab.z, 3),
        "elements": format_numbers(slab.elements.tolist()),
        "element_count": len(slab.elements),
        "supports": len(slab.supports),
        "largest_displacement_mm": _mm(w[low]),
        "largest_displacement_node": int(slab.nodes[low]),
        **part_fields(slab),
    }
    if slab.on_foundation:
        return out | _settlement_summary(
            slab, w, settlement_limit, relative_limit, within_a_wall_from
        )
    if slab.springs:
        out["nodes_on_springs"] = slab.springs
        out["springs_among_supports"] = [slab.points_on_springs, slab.points]  # a body is one
    worst, *others = worst_spans(d, SPANS_SHOWN) or [None]
    if worst is None:
        if slab.held_all_over:
            out["held_all_over"] = True
            out["verdict"] = "no spans: the slab is held at most of its nodes"
        elif len(slab.supports):
            out["verdict"] = "no spans: no line between supports stays inside the slab"
        else:
            out["verdict"] = "no supports found: deflection relative to supports is not defined"
        return out
    limited = fraction is not None
    out |= _reading(slab, w, d, worst, limited)
    if limited:
        out["verdict"] = "ok" if d.lines.ratio[worst] <= 1 else "exceeds the allowed deflection"
    else:
        out["verdict"] = NO_LIMIT
    out["other_spans"] = [_reading(slab, w, d, i, limited) for i in others]
    out["panel_lows"] = [_at(slab, i) | {"displacement_mm": _mm(w[i])} for i in panel_lows(slab, w)]
    return out


def _mm(value: float) -> float:
    return round(float(value) * MM, 2)


def _at(slab: Slab, i: int) -> dict[str, Any]:
    return {
        "node": int(slab.nodes[i]),
        "x_m": round(float(slab.xy[i, 0]), 3),
        "y_m": round(float(slab.xy[i, 1]), 3),
    }


def _reading(slab: Slab, w: np.ndarray, d: Deflections, i: int, limited: bool) -> dict[str, Any]:
    """The worst point of a line: where it is, how its deflection comes out, L / f, and its
    limit when the project set one."""
    lines = d.lines
    a, b = int(lines.a[i]), int(lines.b[i])
    ends = [a, b] if b >= 0 else [a]
    x, y = lines.point[i]
    f = abs(float(lines.f[i]))
    out: dict[str, Any] = {
        "x_m": round(float(x), 3),
        "y_m": round(float(y), 3),
        "nearest_node": int(slab.nodes[np.argmin(np.hypot(*(slab.xy - lines.point[i]).T))]),
        "deflection_mm": _mm(lines.f[i]),
        "span_m": round(float(lines.span[i]), 3),
        "L_to_f": round(float(lines.span[i]) / f) if f > 1e-9 else None,
    }
    if limited:
        out |= {
            "allowed_mm": _mm(lines.allowed[i]),
            "limit_fraction": round(float(lines.span[i] / lines.allowed[i]), 1),  # span / this
            "ratio": round(float(lines.ratio[i]), 3),
            "over_limit": bool(lines.ratio[i] > 1),  # exact ratio: a rounded 1.0 may be either
        }
    return out | {
        "cantilever": b < 0,
        "direction_deg": lines.angle(slab, i),
        "measured_from_nodes": [int(slab.nodes[e]) for e in ends],
        "point_displacement_mm": _mm(lines.w_point[i]),
        "supports_displacement_mm": [_mm(w[e]) for e in ends],
        "line_displacement_mm": _mm(lines.w_line[i]),
    }


def _end(slab: Slab, end: End) -> dict[str, Any]:
    out = {"node": int(slab.nodes[end.node]), "x_m": round(end.x, 3), "y_m": round(end.y, 3)}
    return out | ({"rigid_body_centre": True} if end.centre else {})


def _settlement_summary(
    slab: Slab,
    w: np.ndarray,
    settlement_limit: float | None,
    relative_limit: float | None,
    within_a_wall_from: float,
) -> dict[str, Any]:
    """With no limit (None) nothing is called over it: zones none, the largest difference only."""
    absolute = float("inf") if settlement_limit is None else settlement_limit
    relative_at = float("inf") if relative_limit is None else relative_limit
    # The other end of the range from the largest in size, which the summary has already.
    other = int(np.argmin(w) if w[np.argmax(np.abs(w))] > 0 else np.argmax(w))
    zones = zones_over(slab, w, absolute)
    places = relative_differences(slab, w, relative_at, within_a_wall_from)
    columns = sum(s.body for s in slab.structures)
    too_much = []
    if zones:
        too_much.append("the settlement limit")
    if places and places[0].ratio > relative_at:
        too_much.append("the limit of the relative settlement difference")
    verdict = f"exceeds {' and '.join(too_much)}" if too_much else "ok"
    if settlement_limit is None and relative_limit is None:
        verdict = NO_LIMIT
    if slab.structures and not places:
        verdict += "; one short structure stands on the slab: no relative difference to check"
    elif not places:
        verdict += "; nothing found standing on the slab: relative difference not checked"
    return {
        "on_elastic_foundation": True,
        **({} if slab.on_soil else {"on_separate_supports": True}),
        "node_count": len(slab.nodes),
        "nodes_on_springs": slab.springs,
        **(
            {"of_them_through_rigid_bodies": slab.springs_through_bodies}
            if slab.springs_through_bodies
            else {}
        ),
        **({"plates_on_bedding": slab.bedded} if slab.bedded else {}),
        "smallest_displacement_mm": _mm(w[other]),
        "settlement_limit_mm": None
        if settlement_limit is None
        else round(settlement_limit * MM, 2),
        "zones_over_limit": [_at(slab, i) | {"settlement_mm": _mm(w[i])} for i in zones],
        "relative_settlement": {
            "limit": relative_limit,
            "columns": columns,
            "walls_and_pylons": len(slab.structures) - columns,
            "within_a_wall_from_m": within_a_wall_from,
            "places": [
                {
                    "ratio": round(p.ratio, 5),
                    "over_limit": p.ratio > relative_at,
                    "length_m": round(p.length, 3),
                    "settlements_mm": [_mm(p.a.settlement), _mm(p.b.settlement)],
                    "ends": [_end(slab, p.a), _end(slab, p.b)],
                }
                for p in places
            ],
        },
        "verdict": verdict,
    }


def for_agent(summary: dict[str, Any]) -> dict[str, Any]:
    """A summary with its long lists cut: the report has them whole."""
    out = dict(summary)
    for key, count in (
        ("other_spans", LISTED_TO_AGENT),
        ("panel_lows", LOWS_TO_AGENT),
        ("zones_over_limit", LISTED_TO_AGENT),
    ):
        if key in out and len(out[key]) > count:
            out[f"{key}_total"] = len(out[key])
            out[key] = out[key][:count]
    relative = out.get("relative_settlement")
    if relative and len(relative["places"]) > LISTED_TO_AGENT:
        total = len(relative["places"])
        out["relative_settlement"] = relative | {
            "places": relative["places"][:LISTED_TO_AGENT],
            "places_total": total,
        }
    return out


def deflections(
    slabs: list[Slab], ws: list[np.ndarray], fraction: float | None = None
) -> list[Deflections]:
    return [relative_deflections(s, w, fraction) for s, w in zip(slabs, ws, strict=True)]


def severity(s: dict[str, Any]) -> float:
    """How bad a slab's worst reading is: |f| / fu with a project limit, else f / L."""
    if "ratio" in s:
        return float(s["ratio"])
    return 1.0 / s["L_to_f"] if s.get("L_to_f") else 0.0


def _numbers(cell: Any) -> list[int]:
    return [int(p) for p in str(cell or "").replace(",", " ").split()]


def _flag(cell: Any) -> bool:
    return str(cell).strip() not in ("", "0", "None")


def ru(value: float, digits: int = 2) -> str:
    """A number as a Russian engineer writes it: 8,10; −3,20."""
    shown = round(float(value), digits) + 0.0  # + 0.0: no "−0,00"
    return f"{shown:.{digits}f}".replace(".", ",").replace("-", "−")


def level_text(z: float) -> str:
    return ("+" if z >= 0 else "−") + ru(abs(z), 3)


def deflection_files(
    model_path: str,
    slabs: list[Slab],
    ws: list[np.ndarray],
    found: list[Deflections],
    summaries: list[dict[str, Any]],
    out_dir: Path,
    report: str | None,
    source: str,
    limits: str,
    title: str | None,
    notes: list[str] | None = None,
    keep_pictures: bool = False,
    fraction: float | None = None,
    chosen: bool = False,
    dxf: bool = False,
) -> dict[str, Any]:
    """With ``dxf``, a drawing per slab (``_slab_drawing``). With ``report``, the Word report and
    its PDF; each slab in it is drawn as isofields
    of the deflection relative to supports (mm, on a scale of its own) and its ratio to the
    allowed one, with its dangerous places marked, or of the settlement for a slab on an
    elastic foundation. The PNG stay in ``out_dir`` only with ``keep_pictures``. Runs off
    the COM thread; Word, for the PDF, in a thread of its own."""
    stem = Path(model_path).stem
    with tempfile.TemporaryDirectory() as temp:
        where = out_dir if keep_pictures else Path(temp)
        # A big slab takes minutes to draw: only for what will show the pictures.
        pictures = _isofields(slabs, ws, found, summaries, where, stem) if (
            report or keep_pictures
        ) else {}  # fmt: skip
        files: dict[str, Any] = {}
        if keep_pictures:
            files["pictures"] = [str(p) for ps in pictures.values() for p in ps]
        if dxf:
            drawings = [_slab_drawing(s, w, d, out_dir, stem, fraction is not None)
                        for s, w, d in zip(slabs, ws, found, strict=True)]  # fmt: skip
            files["dxf"] = [str(p) for p in drawings if p is not None]
        if report:
            spans = any("deflection_mm" in s for s in summaries)
            on_foundation = any(s.on_foundation for s in slabs)
            content = Report(
                title or f"Прогибы плит относительно опор — {stem}",
                [
                    f"Модель: {model_path}",
                    f"Перемещения: {source}",
                    *([HOW_DEFLECTION, f"Предельный прогиб: {limits}.", ON_SPANS] if spans else []),
                    *([ON_FOUNDATION] if on_foundation else []),
                ],
                [
                    "Плита",
                    "Отметка, м",
                    "Элементов",
                    "f, мм",
                    "Пролёт, м",
                    "fu, мм",
                    "f/fu" if fraction is not None else "L/f",
                    "Вывод",
                ],
                [_summary_row(s) for s in summaries],
                [_slab_part(s, pictures.get(s["slab"], [])) for s in summaries],
                notes or [],
            )
            name = f"{files_name(stem, slabs, chosen)}_deflections"
            files |= write_reports(content, out_dir / name, report)
    return files


def _deflection_fields(slab: Slab, d: Deflections) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The deflection (mm) and its ratio to the allowed one at the nodes, as drawn, with the
    levels of the deflection: the slab is held at its supports, nothing to read there, and the
    field joins them."""
    f, ratio = d.f * MM, d.ratio.copy()
    f[slab.supports] = 0.0
    ratio[slab.supports] = 0.0
    return f, ratio, plots.nice_levels(float(np.nanmin(f)), max(0.0, float(np.nanmax(f))))


def _slab_drawing(
    slab: Slab, w: np.ndarray, d: Deflections, out_dir: Path, stem: str, limited: bool = True
) -> Path | None:
    """A DXF mosaic on the scale of the slab's isofield, each element with its value: the
    settlement of a slab that settles; the deflection and its ratio to the allowed one, on two
    plans, of one that spans. None for a slab with neither."""
    name = slab_title(slab.number, slab.z, slab.part)
    base = out_dir / f"{stem}_{slab_file(slab)}"
    if slab.on_foundation:
        settled = w * MM
        levels = plots.nice_levels(float(settled.min()), float(settled.max()))
        plans = [
            field_plan(slab, settled, levels, plots.band_colors(len(levels) - 1, True),
                       f"{name}: осадка, мм", "S", "Осадка", "мм"),
        ]  # fmt: skip
        return write_zones(base.with_name(f"{base.name}_settlement.dxf"), slab, outline_loops(slab),
                           plans)  # fmt: skip
    if not worst_spans(d, 1):
        return None
    f, ratio, levels = _deflection_fields(slab, d)
    plans = [
        field_plan(slab, f, levels, plots.band_colors(len(levels) - 1, True),
                   f"{name}: прогиб относительно опор, мм", "F", "Прогиб", "мм"),
    ]  # fmt: skip
    if limited:  # the ratio to a limit only when the project set one
        plans.append(field_plan(slab, ratio, plots.RATIO_LEVELS,
                   [*plots.band_colors(len(plots.RATIO_LEVELS) - 1), plots.OVER_COLOR],
                   f"{name}: отношение прогиба к допустимому, f / fu (показ; вывод — по линиям, "
                   "см. отчёт или ответ)", "RATIO", "f / fu"))  # fmt: skip
    return write_zones(base.with_name(f"{base.name}_deflection.dxf"), slab, outline_loops(slab),
                       plans)  # fmt: skip


def _isofields(
    slabs: list[Slab],
    ws: list[np.ndarray],
    found: list[Deflections],
    summaries: list[dict[str, Any]],
    out_dir: Path,
    stem: str,
) -> dict[int, list[Path]]:
    """The pictures per slab number, each slab on colour scales of its own."""
    pictures: dict[int, list[Path]] = {}
    for slab, w, d, s in zip(slabs, ws, found, summaries, strict=True):
        name = slab_title(slab.number, slab.z, slab.part)
        site = slab.site
        base = out_dir / f"{stem}_{slab_file(slab)}"
        if slab.on_foundation:
            pictures[slab.number] = [
                _settlement_picture(slab, w, s, base.with_name(f"{base.name}_settlement.png"),
                                    name, site)
            ]  # fmt: skip
            continue
        spans = worst_spans(d, SPANS_SHOWN)
        if not spans:
            continue
        f, ratio, levels = _deflection_fields(slab, d)
        lows = [((p["x_m"], p["y_m"]), ru(p["displacement_mm"], 1)) for p in s["panel_lows"]]
        pictures[slab.number] = [
            plots.isofield(
                slab, f, levels, base.with_name(f"{base.name}_deflection.png"),
                f"{name}: прогиб относительно опор", "мм",
                _marks(slab, d, spans), site=site, lows=lows, down_is_red=True,
            ),
        ]  # fmt: skip
        if "ratio" in s:  # the ratio to a limit only when the project set one
            pictures[slab.number].append(plots.isofield(
                slab, ratio, plots.RATIO_LEVELS, base.with_name(f"{base.name}_ratio.png"),
                f"{name}: отношение прогиба к допустимому", "f / fu",
                _marks(slab, d, spans), over_red=bool(np.nanmax(ratio) > 1),
                site=site,
            ))  # fmt: skip
    return pictures


def _settlement_picture(
    slab: Slab, w: np.ndarray, s: dict[str, Any], path: Path, name: str, site: np.ndarray | None
) -> Path:
    """Isofield of the settlement with the zones over its limit and the places of the largest
    relative differences."""
    settled = w * MM
    low = int(np.argmax(np.abs(settled)))
    marks = [plots.Mark(tuple(slab.xy[low]), f"s = {ru(settled[low], 1)} мм")]
    for zone in s["zones_over_limit"]:
        if zone["node"] != slab.nodes[low]:
            text = f"s = {ru(zone['settlement_mm'], 1)} мм"
            marks.append(plots.Mark((zone["x_m"], zone["y_m"]), text))
    places = s["relative_settlement"]["places"]
    for k, p in enumerate(places, start=1):
        line = np.array([[e["x_m"], e["y_m"]] for e in p["ends"]])
        marks.append(plots.Mark(tuple(line.mean(axis=0)), str(k), line, cross=False))
    on_what = "на упругом основании" if slab.on_soil else "на отдельных упругих опорах"
    return plots.isofield(
        slab, settled, plots.nice_levels(float(settled.min()), float(settled.max())), path,
        f"{name}: осадка (плита {on_what})", "мм", marks, site=site,
        limit=s["settlement_limit_mm"], down_is_red=True,
    )  # fmt: skip


def _marks(slab: Slab, d: Deflections, spans: list[int]) -> list[plots.Mark]:
    """Each place by its number: the numbers behind it are in the text beside the picture."""
    lines = d.lines
    return [
        plots.Mark(tuple(lines.point[i]), str(k), np.array([slab.xy[lines.a[i]], lines.end[i]]))
        for k, i in enumerate(spans, start=1)
    ]


# Why Word could not make a PDF in this server session: then later reports go straight
# to the PDF writer instead of waiting for Word again.
_word_trouble: list[str] = []


def write_reports(content: Report, base: Path, report: str) -> dict[str, str]:
    """``base``.docx and / or ``base``.pdf; the PDF by Word from the .docx where Word
    works, otherwise written directly (the answer then says why)."""
    # Not with_suffix: a model named "Block 1.2" would lose ".2_slab_..." from the name.
    docx, pdf = base.parent / f"{base.name}.docx", base.parent / f"{base.name}.pdf"
    out: dict[str, str] = {}
    if report in ("docx", "both") or not _word_trouble:
        to_docx(content, docx)
        if report != "pdf":
            out["docx"] = str(docx)
    if report in ("pdf", "both"):
        if not _word_trouble:
            try:
                word.docx_to_pdf(docx, pdf)
                out["pdf"], out["pdf_by"] = str(pdf), "Microsoft Word"
            except word.WordUnavailable as exc:
                why = str(exc)
                if exc.lasting:  # no Word, or one that asks or hangs: not again
                    _word_trouble.append(why)
        else:
            why = _word_trouble[0]
        if "pdf" not in out:
            to_pdf(content, pdf)
            out["pdf"] = str(pdf)
            out["pdf_by"] = f"the server's PDF writer: {why}"
        if report == "pdf":
            docx.unlink(missing_ok=True)
    return out


def _summary_row(s: dict[str, Any]) -> list[str]:
    head = [str(s["slab"]), level_text(s["z_m"]), str(s["element_count"])]
    if s.get("on_elastic_foundation"):
        return [*head, "—", "—", "—", "—", _foundation_verdict(s)]
    if "deflection_mm" not in s:
        return [*head, "—", "—", "—", "—", "нет пролётов" if s["supports"] else "нет опор"]
    span = ru(s["span_m"]) + (" (консоль)" if s["cantilever"] else "")
    if "ratio" not in s:
        ratio = "—" if s["L_to_f"] is None else str(s["L_to_f"])
        return [*head, ru(s["deflection_mm"]), span, "—", ratio, "предел не задан"]
    verdict = "не превышает" if s["verdict"] == "ok" else "ПРЕВЫШАЕТ"
    return [*head, ru(s["deflection_mm"]), span, ru(s["allowed_mm"]), ru(s["ratio"]), verdict]


def _slab_part(s: dict[str, Any], pictures: list[Path]) -> SlabPart:
    title = slab_title(s["slab"], s["z_m"], bool(s.get("part_of_slab")))
    part = SlabPart(title, pictures=pictures)
    part.lines += part_line(s, s["element_count"])
    part.lines.append(f"Элементов: {s['element_count']}; опорных узлов: {s['supports']}.")
    if s.get("on_elastic_foundation"):
        part.lines += _foundation_lines(s)
        return part
    on_springs, points = s.get("springs_among_supports", (0, 0))
    if on_springs:  # none when a column holds the same rigid body as the spring
        part.lines.append(
            f"Одноузловые упругие опоры — {on_springs} из {points} опор плиты (абсолютно жёсткое "
            "тело считается одной опорой), это меньшая часть: плита считается по прогибу между "
            "опорами, а не по осадке."
        )
    if s.get("held_all_over"):
        part.lines.append(
            "Опоры стоят больше чем у половины узлов, и это не одноузловые упругие опоры: пролётов "
            "между опорами нет, прогиб относительно опор не определён."
        )
        return part
    if "deflection_mm" not in s:
        part.lines.append(
            "Ни одна линия между опорами не проходит внутри плиты: прогиб относительно опор не "
            "определён."
            if s["supports"]
            else "Опоры не найдены: прогиб относительно опор не определён."
        )
        return part
    part.lines += _worst_explained(s)
    part.lines += [_span_line(k, other) for k, other in enumerate(s["other_spans"], start=2)]
    if s["panel_lows"]:
        lowest = s["panel_lows"][0]
        part.lines.append(
            f"Наинизших точек панелей подписано на изополе: {len(s['panel_lows'])}; самая низкая — "
            f"узел {lowest['node']} (X = {ru(lowest['x_m'], 3)} м, Y = {ru(lowest['y_m'], 3)} м), "
            f"перемещение {ru(lowest['displacement_mm'])} мм."
        )
    part.lines.append(
        f"Наибольшее по абсолютной величине перемещение плиты — "
        f"{ru(s['largest_displacement_mm'])} мм (узел {s['largest_displacement_node']})."
    )
    return part


def _worst_explained(s: dict[str, Any]) -> list[str]:
    """How the worst deflection of a slab comes out, with its numbers; the check only when the
    project set a limit."""
    signed, span = s["deflection_mm"], s["span_m"]
    f = abs(signed)
    kind = "расчётном пролёте консоли" if s["cantilever"] else "пролёте"
    share = f", что составляет l/{_fraction(span * MM / f)}" if f > 0 else ""
    lines = [
        f"Самый опасный участок: прогиб f = {ru(signed)} мм на {kind} длиной l = {ru(span)} м "
        f"(участок 1 на изополях){share}.",
        _how_measured(s),
    ]
    if "ratio" not in s:
        return [
            *lines,
            "Предельный прогиб для проекта не задан (СП РК EN 1993-1-1, п. 7.2.1; НП.2.21): "
            "сравнение с пределом выполняет проектировщик.",
        ]
    allowed = s["allowed_mm"]
    exceeds = s["verdict"] != "ok"  # from the exact ratio: a rounded 1,00 may be either
    fraction = _fraction(s["limit_fraction"])
    return [
        *lines,
        f"Предельный прогиб, заданный для проекта: fu = l/{fraction} = {ru(span)} м / {fraction} "
        f"= {ru(allowed)} мм.",
        f"Проверка: |f| / fu = {ru(f)} / {ru(allowed)} = {ru(s['ratio'])} — прогиб "
        + ("превышает предельный." if exceeds else "не превышает предельный."),
    ]


def _how_measured(s: dict[str, Any]) -> str:
    point, f = s["point_displacement_mm"], s["deflection_mm"]
    line = s["line_displacement_mm"]
    where = (
        f"Точка X = {ru(s['x_m'], 3)} м, Y = {ru(s['y_m'], 3)} м (ближайший узел "
        f"{s['nearest_node']})"
    )
    by = s["measured_from_nodes"]
    done = f"f = {ru(point)} − ({ru(line)}) = {ru(f)} мм"
    if s["cantilever"]:
        from_support = abs(line - s["supports_displacement_mm"][0]) < 0.005
        above = "опоры" if from_support else "поверхности через опоры"
        text = (
            f"{where} — на консоли: вылет от опорного узла {by[0]} равен "
            f"{ru(s['span_m'] / 2, 3)} м, расчётный пролёт l = 2 × {ru(s['span_m'] / 2, 3)} = "
            f"{ru(s['span_m'])} м. Перемещение плиты в точке {ru(point)} мм, {above} "
            f"{ru(line)} мм; {done}."
        )
    else:
        ends = s["supports_displacement_mm"]
        text = (
            f"{where} лежит на линии между опорными узлами {by[0]} и {by[1]} под углом "
            f"{ru(s['direction_deg'], 0)}° к оси X. Перемещения опор {ru(ends[0])} и "
            f"{ru(ends[1])} мм; перемещение прямой между ними в этой точке {ru(line)} мм; "
            f"перемещение плиты в точке {ru(point)} мм; {done}."
        )
        above = "прямой между опорами"
    if f > 0:
        text += f" Плита здесь выше {above} (выгиб): сравнивается абсолютная величина."
    return text


def _foundation_verdict(s: dict[str, Any]) -> str:
    """For the narrow last column of the summary table."""
    places = s["relative_settlement"]["places"]
    text = f"осадка: s = {ru(s['largest_displacement_mm'], 1)} мм"
    over = bool(s["zones_over_limit"])
    if places:
        text += f"; Δs/L = {ru(places[0]['ratio'], 4)}"
        over = over or places[0]["over_limit"]
    if s["verdict"] == NO_LIMIT:
        return f"{text}: предел не задан"
    return f"{text}: {'ПРЕВЫШАЕТ' if over else 'не превышает'}"


def _foundation_lines(s: dict[str, Any]) -> list[str]:
    """The settlement of a slab on elastic supports against its limits, with numbers."""
    limit = s["settlement_limit_mm"]
    on_what = (
        "Плита опирается на отдельные упругие опоры (сваи)"
        if s.get("on_separate_supports")
        else "Плита лежит на упругом основании"
    )
    how = []
    if s.get("plates_on_bedding"):
        how.append(
            f"коэффициенты постели у {s['plates_on_bedding']} из {s['element_count']} её элементов"
        )
    if s["nodes_on_springs"]:
        through = s.get("of_them_through_rigid_bodies")
        how.append(
            f"одноузловые упругие опоры у {s['nodes_on_springs']} из {s['node_count']} её узлов"
            + (f" (у {through} из них — через абсолютно жёсткие тела)" if through else "")
        )
    lines = [
        f"{on_what}: {'; '.join(how)}. Пролётов между опорами у неё нет, поэтому прогиб "
        "относительно опор не применяется.",
        f"Осадка — от {ru(s['smallest_displacement_mm'])} до "
        f"{ru(s['largest_displacement_mm'])} мм (наибольшая по абсолютной величине в узле "
        f"{s['largest_displacement_node']}); "
        + (
            "предельная осадка не задана."
            if limit is None
            else f"предельная по абсолютной величине {ru(limit)} мм — "
            + ("превышена." if s["zones_over_limit"] else "не превышена.")
        ),
    ]
    lines += [
        f"Зона {k} за пределом: наибольшее перемещение {ru(z['settlement_mm'])} мм в узле "
        f"{z['node']} (X = {ru(z['x_m'], 3)} м, Y = {ru(z['y_m'], 3)} м)."
        for k, z in enumerate(s["zones_over_limit"], start=1)
    ]
    relative = s["relative_settlement"]
    from_m = f"{relative['within_a_wall_from_m']:g}".replace(".", ",")
    if not relative["places"]:
        lines.append(
            f"На плите стоит одна конструкция короче {from_m} м: относительную разность осадок "
            "сравнивать не с чем."
            if relative["columns"] + relative["walls_and_pylons"]
            else "Вертикальные конструкции, опирающиеся на плиту, не найдены: относительная "
            "разность осадок не проверена."
        )
        return lines
    lines.append(
        f"Относительная разность осадок проверена между конструкциями на плите (колонн — "
        f"{relative['columns']}, стен и пилонов — {relative['walls_and_pylons']}); внутри одной "
        f"стены — между точками не ближе {from_m} м. "
        + (
            "Предел не задан."
            if relative["limit"] is None
            else f"Предел {ru(relative['limit'], 4)}."
        )
    )
    for k, p in enumerate(relative["places"], start=1):
        a, b = p["ends"]
        s1, s2 = p["settlements_mm"]
        lines.append(
            f"Участок {k}: {_end_text(a)} и {_end_text(b)}: "
            f"Δs/L = |{ru(s1)} − ({ru(s2)})| мм / {ru(p['length_m'] * MM, 0)} мм = "
            f"{ru(p['ratio'], 4)}"
            + (
                "."
                if relative["limit"] is None
                else " — " + ("превышает предел." if p["over_limit"] else "не превышает предел.")
            )
        )
    return lines


def _end_text(end: dict[str, Any]) -> str:
    what = (
        f"центр АЖТ у узла {end['node']}" if end.get("rigid_body_centre") else f"узел {end['node']}"
    )
    return f"{what} (X = {ru(end['x_m'], 3)} м, Y = {ru(end['y_m'], 3)} м)"


def _fraction(n: float) -> str:
    """The n of span / n, a whole number where it nearly is."""
    return f"{n:.0f}" if abs(n - round(n)) < 0.05 else ru(n, 1)


def _span_line(k: int, s: dict[str, Any]) -> str:
    by = " и ".join(map(str, s["measured_from_nodes"]))
    kind = "консоль, расчётный пролёт" if s["cantilever"] else "пролёт"
    above = " (выгиб)" if s["deflection_mm"] > 0 else ""
    head = (
        f"Участок {k}: {kind} l = {ru(s['span_m'])} м от опорных узлов {by}, точка X = "
        f"{ru(s['x_m'], 3)} м, Y = {ru(s['y_m'], 3)} м: f = {ru(s['deflection_mm'])} мм{above}"
    )
    if "ratio" not in s:
        return head + (f", l/f = {s['L_to_f']}." if s["L_to_f"] else ".")
    verdict = "превышает предельный" if s["over_limit"] else "не превышает предельный"
    return (
        f"{head}; fu = {ru(s['span_m'])} м / {_fraction(s['limit_fraction'])} = "
        f"{ru(s['allowed_mm'])} мм; |f| / fu = {ru(s['ratio'])} — {verdict}."
    )


def cracked_copy(
    adapter: Any,
    model: str | None,
    factor: float,
    elements: list[int] | None,
    save_as: str | None,
    replace: bool,
    backup: Any = None,
    tolerance: float = LEVEL_TOLERANCE,
) -> dict[str, Any]:
    """On the COM thread: a copy of a saved model beside it (or at ``save_as``) whose
    slab plates - or ``elements`` - have the factor kE to their elastic modulus, for
    deflections with cracks. The original is not changed; the copy is left open.
    ``backup(path)`` keeps a file the copy replaces."""
    if not 0 < factor <= 1:
        raise LiraInputError("factor: kE for cracks reduces the modulus: 0 < kE <= 1")
    if model and model.lower().endswith(".lir"):
        try:
            info = adapter.document_info(model)
        except LiraInputError:
            info = None  # not open: the file is copied as it is
    else:
        info = adapter.document_info(model)
    source = Path(info.path if info else str(model))
    if info is not None and (not info.saved or info.modified):
        raise LiraInputError(f"Save {info.title} first: the copy is made from the file on disk")
    if not source.is_file():
        raise LiraInputError(f"File not found: {source}")
    kE = f"{factor:g}".replace(".", "_")  # LIRA refuses a dot in a model name
    target = Path(save_as) if save_as else source.with_name(f"{source.stem}_kE{kE}.lir")
    if target.suffix.lower() != ".lir" or not target.is_absolute():
        raise LiraInputError("save_as: an absolute path of a .lir file")
    if why := bad_model_name(str(target)):
        raise LiraInputError(why)
    if target.stem.casefold() == source.stem.casefold():
        # LIRA keeps results by model name: the copy's analysis would replace the
        # original's results, wherever the copy lies.
        raise LiraInputError("save_as must have a name other than the model's own")
    replaced = None
    if target.exists():
        if not replace:
            raise LiraInputError(f"{target} exists: pass replace=true or another save_as")
        replaced = backup(str(target)) if backup else None
    try:  # an older copy open in LIRA
        open_copy = adapter.document_info(str(target))
    except LiraInputError:
        open_copy = None
    if open_copy is not None:
        if open_copy.modified:
            raise LiraInputError(
                f"{target} is open in LIRA with unsaved changes: save or close it first"
            )
        adapter.close_document(str(target), True)
    shutil.copy2(source, target)
    copy, _ = adapter.open_document(str(target))
    if elements is None:
        mesh = read_mesh(adapter, copy.path)
        elements = sorted(int(e) for s in slabs_of(mesh, None, tolerance) for e in s.elements)
    before = {
        int(r[0]): float(r[1])
        for r in adapter.read_table(copy.path, "Stiffnesses_Factors", elements)
        if len(r) > 1 and r[0] is not None and r[1] not in (None, "")
    }
    errors = adapter.write_table(
        copy.path, "Stiffnesses_Factors", [[e, factor] for e in elements], elements
    )
    if errors:
        adapter.close_document(copy.path, True)
        raise LiraInputError(
            f"LIRA rejected kE for the copy: {errors[0].message} (the copy {target} is left "
            "unchanged)"
        )
    adapter.save_document(copy.path)
    changed = {e: k for e, k in before.items() if abs(k - 1) > 1e-9 and abs(k - factor) > 1e-9}
    out: dict[str, Any] = {
        "model": str(source),
        "copy": str(target),
        "factor": factor,
        "elements": format_numbers(elements),
        "element_count": len(elements),
    }
    if replaced:
        out["backup_of_replaced_file"] = str(replaced)
    if changed:
        out["replaced_factors"] = {
            "count": len(changed),
            "values": sorted(set(changed.values()))[:10],
        }
    return out
