"""Service: checks of a model before its explanatory note (RPZ) is written. Each finds a
mistake that would make the note's numbers wrong or incomplete; none changes the model."""

from collections import Counter
from typing import Any

import numpy as np

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services import rpz
from lira_sapr_mcp.services.models import results_summary
from lira_sapr_mcp.services.slabs import TO_METERS, open_model

PILE = 57  # FE 57 keeps stiffness 0 in Elements_Stiffnesses: its stiffness lives elsewhere
SAME_PLACE = 0.001  # m: nodes nearer than this are one place
LISTED = 20  # numbers listed per finding


def _numbers(cell: Any) -> list[int]:
    return [
        int(x) for x in str(cell or "").replace(",", " ").split() if x.strip().lstrip("-").isdigit()
    ]


def unused_nodes(nodes: set[int], elements: dict[int, tuple[int, ...]]) -> list[int]:
    used = {n for ns in elements.values() for n in ns}
    return sorted(nodes - used)


def coincident_nodes(
    xyz: dict[int, tuple[float, float, float]], tolerance: float = SAME_PLACE
) -> list[list[int]]:
    """Groups of nodes at one place (within ``tolerance`` m), each sorted."""
    if not xyz:
        return []
    ids = np.array(sorted(xyz))
    p = np.array([xyz[i] for i in ids])
    keys = np.round(p / tolerance).astype(np.int64)
    groups: dict[tuple, list[int]] = {}
    for i, k in zip(ids.tolist(), map(tuple, keys), strict=True):
        groups.setdefault(k, []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def without_stiffness(types: dict[int, int], stiffness_of: dict[int, int]) -> list[int]:
    """Elements with no stiffness or stiffness 0, piles (FE 57) aside."""
    return sorted(
        el for el, fe in types.items() if fe % 100 != PILE and stiffness_of.get(el, 0) == 0
    )


def _finding(what: str, ru: str, items: list, ok: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"check": what, "ok": ok or not items, "ru": ru}
    if items:
        out["count"] = len(items)
        out["items"] = items[:LISTED]
    return out


def check_model(adapter: Any, model: str | None) -> dict[str, Any]:
    """On the COM thread: every check, ok or what it found."""
    path = open_model(adapter, model)
    scale = TO_METERS.get(adapter.units().geometry, 1.0)
    xyz = {
        int(r[0]): (float(r[1]) * scale, float(r[2]) * scale, float(r[3]) * scale)
        for r in rpz._rows(adapter, path, "Nodes_Coordinates")
        if len(r) >= 4 and None not in r[:4]
    }
    elements: dict[int, tuple[int, ...]] = {}
    types: dict[int, int] = {}
    for r in rpz._rows(adapter, path, "Elements_TypeAndNodes"):
        if len(r) >= 3 and None not in r[:3]:
            elements[int(r[0])] = tuple(_numbers(r[2]))
            types[int(r[0])] = int(r[1])
    stiffness_of = {
        int(r[0]): int(r[1])
        for r in rpz._rows(adapter, path, "Elements_Stiffnesses")
        if len(r) > 1 and r[0] is not None and r[1] is not None
    }
    findings = []
    results = results_summary(adapter, path, len(xyz), len(elements))
    findings.append(
        _finding(
            "results present and of this model",
            "Результаты расчёта есть и относятся к этой модели",
            []
            if results.get("present") and "note" not in results
            else [results.get("note", "нет результатов")],
        )
    )
    warning = adapter.results_warning(path)
    findings.append(
        _finding(
            "results not older than the model",
            "Модель не сохранялась после расчёта",
            [warning] if warning else [],
        )
    )
    findings.append(
        _finding(
            "every element has a stiffness",
            "У всех элементов есть жёсткость (сваи КЭ 57 не в счёт)",
            without_stiffness(types, stiffness_of),
        )
    )
    findings.append(
        _finding(
            "no node outside elements",
            "Нет узлов, не входящих ни в один элемент",
            unused_nodes(set(xyz), elements),
        )
    )
    findings.append(
        _finding(
            "no coincident nodes",
            "Нет совпадающих узлов (ближе 1 мм)",
            coincident_nodes(xyz),
        )
    )
    cases = [int(r[0]) for r in rpz._rows(adapter, path, "LoadCases") if r and r[0] is not None]
    try:
        infos = adapter.load_combinations(path)["combinations"]
    except LiraInputError:
        infos = []
    if not infos:
        findings.append(
            _finding("the model has load combinations (RSN)", "В модели заданы РСН", ["нет РСН"])
        )
    elif results.get("present"):
        fits, dynamic = rpz.multipliers(adapter, path)
        used = {
            c for f in fits.values() for c, k in zip(f.cases, f.multipliers, strict=True) if k != 0
        } | dynamic
        findings.append(
            _finding(
                "every load case is in some RSN",
                "Каждое загружение входит хотя бы в одно РСН",
                sorted(set(cases) - used),
            )
        )
    kinds = Counter(fe % 100 for fe in types.values())
    return {
        "model": path,
        "nodes": len(xyz),
        "elements": len(elements),
        "fe_types": {str(k): v for k, v in sorted(kinds.items())},
        "all_ok": all(f["ok"] for f in findings),
        "findings": findings,
    }
