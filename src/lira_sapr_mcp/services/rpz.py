"""Service: facts of a model for the explanatory note (RPZ) — load cases, the make-up of the
load combinations (RSN), stiffnesses with their elements — and support reactions with the
pressure under footings by SP RK 5.01-102-2013."""

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing.footing import Fit, fit_combination, pressure
from lira_sapr_mcp.services.slabs import open_model

SAMPLE = 300  # nodes whose displacements fit the RSN multipliers
DOFS = ("X", "Y", "Z", "UX", "UY", "UZ")
GOOD_FIT = 1e-6  # relative residual below which the multipliers are exact
FORCES = ("RX", "RY", "RZ", "RUX", "RUY", "RUZ")


def _rows(adapter: Any, path: str, table: str) -> list[list]:
    try:
        return adapter.read_table(path, table)
    except Exception:
        return []


def _sample(adapter: Any, path: str) -> list[int]:
    nodes = sorted(
        int(r[0]) for r in _rows(adapter, path, "Nodes_Coordinates") if r and r[0] is not None
    )
    if len(nodes) <= SAMPLE:
        return nodes
    step = len(nodes) / SAMPLE
    return sorted({nodes[int(i * step)] for i in range(SAMPLE)})


def _static_cases(rows: list[dict]) -> tuple[dict[int, dict[int, np.ndarray]], set[int]]:
    """Per static load case, per node, the six displacements; and the dynamic cases left out."""
    out: dict[int, dict[int, np.ndarray]] = {}
    dynamic: set[int] = set()
    for r in rows:
        case = int(r["case"])
        if "shape" in r:
            dynamic.add(case)
            continue
        out.setdefault(case, {})[int(r["node"])] = np.array([float(r.get(k, 0.0)) for k in DOFS])
    for case in dynamic:
        out.pop(case, None)
    return out, dynamic


def multipliers(
    adapter: Any, path: str, combinations: list[int] | None = None
) -> tuple[dict[int, Fit], set[int]]:
    """On the COM thread: per RSN, the multiplier of each static load case, fitted from the
    displacements of up to SAMPLE nodes; and the dynamic cases that could not take part."""
    nodes = _sample(adapter, path)
    by_case, dynamic = _static_cases(adapter.node_displacements(path, nodes, None))
    if not by_case:
        raise LiraInputError("No static load case results: run lira_run_analysis")
    rows = adapter.combination_displacements(path, nodes, combinations)
    by_rsn: dict[int, dict[int, np.ndarray]] = {}
    for r in rows:
        by_rsn.setdefault(int(r["combination"]), {})[int(r["node"])] = np.array(
            [float(r.get(k, 0.0)) for k in DOFS]
        )
    fits = {}
    for rsn, disp in sorted(by_rsn.items()):
        common = sorted(set(disp).intersection(*(set(v) for v in by_case.values())))
        a = {c: np.array([by_case[c][n] for n in common]) for c in by_case}
        fits[rsn] = fit_combination(a, np.array([disp[n] for n in common]))
    return fits, dynamic


def model_facts(adapter: Any, model: str | None) -> dict[str, Any]:
    """On the COM thread: load cases, RSN with their make-up, stiffnesses and elements."""
    path = open_model(adapter, model)
    cases = [
        {"number": int(r[0]), "name": r[1], "kind": r[3] if len(r) > 3 else None}
        for r in _rows(adapter, path, "LoadCases")
        if r and r[0] is not None
    ]
    names = {c["number"]: c["name"] for c in cases}
    count = Counter(
        int(r[1])
        for r in _rows(adapter, path, "Elements_Stiffnesses")
        if len(r) > 1 and r[1] is not None
    )
    stiffnesses = [
        {
            "number": int(r[0]),
            "parameters": next((c for c in r[3:] if c), None),
            "elements": count.get(int(r[0]), 0),
        }
        for r in _rows(adapter, path, "Stiffnesses")
        if r and r[0] is not None
    ]
    out: dict[str, Any] = {"model": path, "load_cases": cases, "stiffnesses": stiffnesses}
    try:
        infos = adapter.load_combinations(path)
    except LiraInputError:
        infos = {"combinations": []}
    if infos["combinations"]:
        fits, dynamic = multipliers(adapter, path)
        rsn = []
        for c in infos["combinations"]:
            fit = fits.get(c["number"])
            row = {
                "number": c["number"],
                "name": c["name"],
                "special": c["special"],
                "seismic": c["seismic"],
            }
            if fit is not None:
                row["make_up"] = [
                    {"case": case, "name": names.get(case), "multiplier": round(k, 4)}
                    for case, k in zip(fit.cases, fit.multipliers, strict=True)
                    if k != 0
                ]
                row["exact"] = fit.residual < GOOD_FIT
                row["residual"] = float(f"{fit.residual:.2e}")
            rsn.append(row)
        out["combinations"] = rsn
        if dynamic:
            out["dynamic_cases_left_out"] = sorted(dynamic)
    return out


def reactions(
    adapter: Any,
    model: str | None,
    nodes: list[int],
    case: int | None,
    combination: int | None,
) -> dict[int, dict[str, float]]:
    """On the COM thread: per node, the loads the model's fragment passes to it (LIRA's loads
    on fragment), for a load case or an RSN (its fitted multipliers times the load cases')."""
    path = open_model(adapter, model)
    if combination is None:
        rows = adapter.fragment_loads(path, nodes, [case])
        return {
            int(r["node"]): {k: float(r.get(k, 0.0)) for k in FORCES}
            for r in rows
            if "shape" not in r
        }
    fits, dynamic = multipliers(adapter, path, [combination])
    fit = fits.get(combination)
    if fit is None:
        raise LiraInputError(f"No load combination {combination}")
    if fit.residual >= GOOD_FIT:
        raise LiraInputError(
            f"RSN {combination} is not a sum of static load cases (residual {fit.residual:.1e}"
            + (f"; dynamic cases {sorted(dynamic)} are in it" if dynamic else "")
            + "): give the load cases one by one"
        )
    used = [(c, k) for c, k in zip(fit.cases, fit.multipliers, strict=True) if k != 0]
    rows = adapter.fragment_loads(path, nodes, [c for c, _ in used])
    out: dict[int, dict[str, float]] = {n: dict.fromkeys(FORCES, 0.0) for n in nodes}
    k = dict(used)
    for r in rows:
        if "shape" in r or int(r["case"]) not in k:
            continue
        acc = out.setdefault(int(r["node"]), dict.fromkeys(FORCES, 0.0))
        for f in FORCES:
            acc[f] += k[int(r["case"])] * float(r.get(f, 0.0))
    return out


def footing_checks(
    footings: list[dict[str, Any]], forces: dict[int, dict[str, float]]
) -> list[dict]:
    """Each footing: its nodes' loads summed (N = |RZ|, moments by size) and the pressure."""
    out = []
    for f in footings:
        nodes = f["nodes"]
        missing = [n for n in nodes if n not in forces]
        if missing:
            raise LiraInputError(
                f"Footing {f.get('name') or nodes}: no loads on fragment at nodes {missing}: "
                "the fragment must hold them"
            )
        rz = sum(forces[n]["RZ"] for n in nodes)
        mx = sum(forces[n]["RUX"] for n in nodes)
        my = sum(forces[n]["RUY"] for n in nodes)
        try:
            p = pressure(
                abs(rz),
                mx,
                my,
                f["l"],
                f["b"],
                f["d"],
                f["R"],
                f.get("gamma_mt", 20.0),
                bool(f.get("trapezoid", False)),
            )
        except ValueError as exc:
            raise LiraInputError(f"Footing {f.get('name') or nodes}: {exc}") from exc
        out.append(
            {
                "name": f.get("name") or ", ".join(map(str, nodes)),
                "nodes": nodes,
                "N_kN": round(abs(rz), 1),
                "Mx_kNm": round(mx, 1),
                "My_kNm": round(my, 1),
                "l_m": f["l"],
                "b_m": f["b"],
                "d_m": f["d"],
                "R_kPa": f["R"],
                "p_kPa": round(p.p, 1),
                "p_max_kPa": round(p.p_max, 1),
                "p_min_kPa": round(p.p_min, 1),
                "checks": [{"check": w, "ok": ok, "numbers": s} for w, ok, s in p.checks],
                "ok": p.ok,
            }
        )
    return out


def write_facts(facts: dict[str, Any], out_dir: Path) -> str:
    """A Word file with the tables for sections 1.7 and 4 of the RPZ."""
    from docx import Document

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph("Загружения расчётной схемы")
    t = doc.add_table(rows=1, cols=3)
    t.style = "Table Grid"
    for cell, h in zip(t.rows[0].cells, ("№", "Наименование", "Вид"), strict=True):
        cell.text = h
    for c in facts["load_cases"]:
        for cell, v in zip(t.add_row().cells, (c["number"], c["name"], c["kind"]), strict=True):
            cell.text = "" if v is None else str(v)
    if facts.get("combinations"):
        doc.add_paragraph("Расчётные сочетания нагрузок (РСН): множители к результатам загружений")
        t = doc.add_table(rows=1, cols=3)
        t.style = "Table Grid"
        for cell, h in zip(t.rows[0].cells, ("№", "Наименование", "Состав"), strict=True):
            cell.text = h
        for c in facts["combinations"]:
            parts = " + ".join(
                f"{str(m['multiplier']).replace('.', ',')}·L{m['case']}"
                for m in c.get("make_up", [])
            )
            if c.get("make_up") is not None and not c.get("exact"):
                parts += " (приближённо: РСН содержит динамику или не сводится к сумме)"
            for cell, v in zip(
                t.add_row().cells, (c["number"], c["name"], parts or "—"), strict=True
            ):
                cell.text = str(v)
    doc.add_paragraph("Жёсткости")
    t = doc.add_table(rows=1, cols=3)
    t.style = "Table Grid"
    for cell, h in zip(t.rows[0].cells, ("№", "Параметры", "Элементов"), strict=True):
        cell.text = h
    for s in facts["stiffnesses"]:
        for cell, v in zip(
            t.add_row().cells, (s["number"], s["parameters"], s["elements"]), strict=True
        ):
            cell.text = "" if v is None else str(v)
    path = out_dir / f"{Path(facts['model']).stem}_rpz_facts.docx"
    doc.save(path)
    return str(path)


def write_loads(results: list[Any], out_dir: Path, name: str) -> str:
    """Section 3 of the RPZ: each calculation with its lines, then the table of loads (kg/m²)."""
    from docx import Document

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = Document()
    for r in results:
        doc.add_paragraph(r.title).runs[0].bold = True
        for line in r.lines:
            doc.add_paragraph(line)
    doc.add_paragraph("Сводка нормативных нагрузок (без коэффициента надёжности)").runs[
        0
    ].bold = True
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Нагрузка", "Значение, кг/м²"
    for r in results:
        for what, value in r.loads:
            cells = t.add_row().cells
            cells[0].text, cells[1].text = what, f"{value:g}".replace(".", ",")
    path = out_dir / f"{name}_нагрузки.docx"
    doc.save(path)
    return str(path)
