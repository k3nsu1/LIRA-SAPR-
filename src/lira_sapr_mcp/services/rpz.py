"""Service: facts of a model for the explanatory note (RPZ) — load cases, the make-up of the
load combinations (RSN), stiffnesses with their elements — and support reactions with the
pressure under footings by SP RK 5.01-102-2013."""

from collections import Counter
from dataclasses import asdict
from math import isclose, isfinite
from pathlib import Path
from typing import Any

import numpy as np

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing.footing import Fit, fit_combination, pressure
from lira_sapr_mcp.services.slabs import TO_METERS, open_model

SAMPLE = 300  # nodes evenly over the model whose displacements fit the RSN multipliers
PEAK = 10  # and, per static load case, the nodes it moves most
DOFS = ("X", "Y", "Z", "UX", "UY", "UZ")
GOOD_FIT = 1e-6  # relative residual below which the multipliers are exact
FORCES = ("RX", "RY", "RZ", "RUX", "RUY", "RUZ")
KN_PER_TF = 9.80665  # LIRA's results API gives forces in tf and moments in tf·m, always


def _rows(adapter: Any, path: str, table: str) -> list[list]:
    try:
        return adapter.read_table(path, table)
    except Exception:
        return []


def _sample(adapter: Any, path: str) -> list[int]:
    """Nodes whose displacements fit the RSN multipliers: up to SAMPLE nodes evenly over the
    node numbers, and the PEAK nodes each static load case moves most. A load that acts on a
    small part of the model (a canopy over a large slab) moves few nodes; the even sample may
    miss all of them, and the fit then cannot see that load case at all."""
    nodes = sorted(
        int(r[0]) for r in _rows(adapter, path, "Nodes_Coordinates") if r and r[0] is not None
    )
    if len(nodes) <= SAMPLE:
        return nodes
    step = len(nodes) / SAMPLE
    chosen = {nodes[int(i * step)] for i in range(SAMPLE)}
    cases = [int(r[0]) for r in _rows(adapter, path, "LoadCases") if r and r[0] is not None]
    for case in cases:  # one case at a time: all nodes of all cases may not fit in memory
        try:
            rows = adapter.node_displacements(path, None, [case])
        except LiraInputError:
            continue
        size = {}
        for r in rows:
            if "shape" in r or int(r["case"]) != case:
                continue
            size[int(r["node"])] = max(abs(float(r.get(k) or 0.0)) for k in DOFS[:3])
        moved = sorted((n for n, v in size.items() if v > 0), key=size.get, reverse=True)
        chosen.update(moved[:PEAK])
    return sorted(chosen)


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


def _dcl_factors(rows: list[list]) -> dict[int, dict[str, float]]:
    """Per load case, γf and the long-term share from the DCL input table: number, name,
    kind, (empty), sign, exclusion group, γf, long-term share, ... (K2, examples 01 and
    04). DCL has no coefficient of a load case in each RSN. Rows it cannot read are left
    out rather than guessed."""
    out = {}
    for r in rows:
        try:
            out[int(r[0])] = {"gamma_f": float(r[6]), "long_term_share": float(r[7])}
        except (TypeError, ValueError, IndexError):
            continue
    return out


def model_facts(adapter: Any, model: str | None) -> dict[str, Any]:
    """On the COM thread: load cases, RSN with their make-up, stiffnesses and elements."""
    path = open_model(adapter, model)
    cases = [
        {"number": int(r[0]), "name": r[1], "kind": r[3] if len(r) > 3 else None}
        for r in _rows(adapter, path, "LoadCases")
        if r and r[0] is not None
    ]
    factors = _dcl_factors(_rows(adapter, path, "DCL"))
    for c in cases:
        c.update(factors.get(c["number"], {}))
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
                    if round(k, 4) != 0
                ]
                row["exact"] = (
                    fit.identifiable and not fit.discarded_cases and fit.residual < GOOD_FIT
                )
                row["ambiguous_cases"] = fit.ambiguous_cases
                if not fit.identifiable:
                    row["warning"] = (
                        "Множители загружений по отдельности не определены: "
                        + ", ".join(map(str, fit.ambiguous_cases))
                    )
                    row["make_up"] = []
                if fit.discarded_cases:
                    row["discarded_cases"] = fit.discarded_cases
                    warning = "Малый вклад в перемещения: отброшены ненулевые множители загружений "
                    warning += ", ".join(map(str, fit.discarded_cases))
                    row["warning"] = " ".join(filter(None, (row.get("warning"), warning)))
                    row["make_up"] = []
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
    on fragment), for a load case or an RSN (its fitted multipliers times the load cases'),
    in kN and kN·m: LIRA's results API answers in tf and tf·m whatever units LIRA shows."""
    path = open_model(adapter, model)
    wanted = set(nodes)
    unit = adapter.units().geometry
    if unit not in TO_METERS:
        raise LiraInputError(f"Unknown geometry unit {unit!r} in LIRA")
    for row in adapter.read_table(path, "Nodes_LocalAxes", nodes):
        if row and row[0] is not None and int(row[0]) in wanted:
            raise LiraInputError(f"Node {row[0]} has local axes; global reactions are required")
    positions = {
        int(row[0]): [float(value) * TO_METERS[unit] for value in row[1:4]]
        for row in adapter.read_table(path, "Nodes_Coordinates", nodes)
        if len(row) >= 4
        and row[0] is not None
        and int(row[0]) in wanted
        and all(value is not None for value in row[1:4])
    }
    if wanted - positions.keys():
        raise LiraInputError(f"Missing coordinates for nodes {sorted(wanted - positions.keys())}")
    if combination is None:
        weights = {case: 1.0}
    else:
        fits, dynamic = multipliers(adapter, path, [combination])
        fit = fits.get(combination)
        if fit is None:
            raise LiraInputError(f"No load combination {combination}")
        if not fit.identifiable:
            raise LiraInputError(
                f"RSN {combination}: multipliers not uniquely determined; "
                f"indistinguishable load cases {fit.ambiguous_cases}"
            )
        if fit.discarded_cases:
            raise LiraInputError(
                f"RSN {combination}: fit discarded nonzero multipliers for load cases "
                f"{fit.discarded_cases}; reactions cannot be inferred"
            )
        if fit.residual >= GOOD_FIT:
            raise LiraInputError(
                f"RSN {combination} is not a sum of static load cases (residual {fit.residual:.1e}"
                + (f"; dynamic cases {sorted(dynamic)} are in it" if dynamic else "")
                + "): give the load cases one by one"
            )
        weights = {c: k for c, k in zip(fit.cases, fit.multipliers, strict=True) if k != 0}
    if not weights:
        raise LiraInputError(f"RSN {combination}: no contributing static load case")
    rows = adapter.fragment_loads(path, nodes, list(weights))
    out = {n: dict.fromkeys(FORCES, 0.0) for n in nodes}
    seen = set()
    for row in rows:
        node, load_case = int(row["node"]), int(row["case"])
        if "shape" in row or node not in wanted or load_case not in weights:
            continue
        if (node, load_case) in seen:
            raise LiraInputError(f"Duplicate fragment load for node {node}, case {load_case}")
        seen.add((node, load_case))
        for component in FORCES:
            out[node][component] += float(row.get(component, 0.0)) * KN_PER_TF * weights[load_case]
    for node in nodes:
        for load_case in weights:
            if (node, load_case) not in seen:
                raise LiraInputError(f"No loads on fragment for node {node}, case {load_case}")
        out[node].update(zip(("x_m", "y_m", "z_m"), positions[node], strict=True))
        if not all(isfinite(value) for value in out[node].values()):
            raise LiraInputError(f"Non-finite coordinate or reaction for node {node}")
    return out


def footing_checks(
    footings: list[dict[str, Any]],
    forces: dict[int, dict[str, float]],
    compression_rz_sign: int | None = None,
) -> list[dict]:
    """Transfer each node's global loads to its footing base, then check soil pressure."""
    if compression_rz_sign not in (None, -1, 1) or isinstance(compression_rz_sign, bool):
        raise LiraInputError("compression_rz_sign must be -1 or +1")
    out = []
    for f in footings:
        nodes = f["nodes"]
        if not nodes or len(nodes) != len(set(nodes)):
            raise LiraInputError("Footing needs nodes without duplicates")
        missing = [n for n in nodes if n not in forces]
        if missing:
            raise LiraInputError(
                f"Footing {f.get('name') or nodes}: no loads on fragment at nodes {missing}: "
                "the fragment must hold them"
            )
        loads = [forces[n] for n in nodes]
        if any(not isclose(row["z_m"], loads[0]["z_m"], rel_tol=0, abs_tol=1e-6) for row in loads):
            raise LiraInputError("Footing nodes must be at the same elevation for a common h_node")
        if "center" in f:
            center = f["center"]
            if not isinstance(center, (list, tuple)) or len(center) != 2:
                raise LiraInputError("center must be [X, Y] in metres")
            cx, cy = center
        else:
            cx, cy = (sum(row[axis] for row in loads) / len(nodes) for axis in ("x_m", "y_m"))
        h = f.get("h_node", 0.0)
        length, width, depth, resistance = f["l"], f["b"], f["d"], f["R"]
        gamma = f.get("gamma_mt", 20.0)
        if not all(
            isinstance(v, (float, int)) and isfinite(v)
            for v in (cx, cy, h, length, width, depth, resistance, gamma)
        ):
            raise LiraInputError(
                "Footing dimensions, center, height and soil parameters must be finite numbers"
            )
        if h < 0:
            raise LiraInputError("h_node must be nonnegative")
        if min(length, width, resistance) <= 0 or min(depth, gamma) < 0:
            raise LiraInputError("l, b and R must be positive; d and gamma_mt must be nonnegative")
        qx, qy, rz = (sum(row[key] for row in loads) for key in ("RX", "RY", "RZ"))
        mx = sum(row["RUX"] + (row["y_m"] - cy) * row["RZ"] - h * row["RY"] for row in loads)
        my = sum(row["RUY"] + h * row["RX"] - (row["x_m"] - cx) * row["RZ"] for row in loads)
        mz = sum(
            row["RUZ"] + (row["x_m"] - cx) * row["RY"] - (row["y_m"] - cy) * row["RX"]
            for row in loads
        )
        n = compression_rz_sign * rz if compression_rz_sign is not None else None
        total = n + gamma * depth * length * width if n is not None else None
        result = {
            "name": f.get("name") or ", ".join(map(str, nodes)),
            "nodes": nodes,
            "center_m": [cx, cy],
            "center_source": "given" if "center" in f else "mean_node_coordinates",
            "h_node_m": h,
            "height_source": "given" if "h_node" in f else "nodes_at_base_by_owner_default",
            "RZ_kN": rz,
            "compression_rz_sign": compression_rz_sign,
            "N_kN": round(n, 1) if n is not None else None,
            "N_total_kN": total,
            "Qx_kN": qx,
            "Qy_kN": qy,
            "Mx_kNm": round(mx, 1),
            "My_kNm": round(my, 1),
            "Mz_kNm": round(mz, 1),
            "l_m": length,
            "b_m": width,
            "d_m": depth,
            "R_kPa": resistance,
            "p_kPa": None,
            "p_max_kPa": None,
            "p_min_kPa": None,
            "checks": [],
            "ok": False,
        }
        if qx or qy:
            result.update(
                dMx_dh_kN=-qy,
                dMy_dh_kN=qx,
                height_note="Per extra metre, add -Qy to Mx and +Qx to My (kN·m).",
            )
        out.append(result)
        if n is None:
            result["status"] = "sign_unverified"
            result["message"] = (
                "Знак RZ при сжатии не подтверждён. Укажите проверенный compression_rz_sign."
            )
            continue
        if total <= 0:
            result["status"] = "uplift"
            result["message"] = "Отрыв: с учётом веса сжатия нет, проверка давления неприменима."
            continue
        # The raw right-hand moment of vertical force is (y*Fz, -x*Fz).
        # The contact solver instead uses the first moments of positive compression.
        p = pressure(
            n,
            compression_rz_sign * mx,
            -compression_rz_sign * my,
            length,
            width,
            depth,
            resistance,
            gamma,
            bool(f.get("trapezoid", False)),
        )
        result.update(
            status="checked",
            p_kPa=round(p.p, 1),
            p_max_kPa=round(p.p_max, 1) if p.p_max is not None else None,
            p_min_kPa=round(p.p_min, 1) if p.p_min is not None else None,
            checks=[{"check": w, "ok": ok, "numbers": msg} for w, ok, msg in p.checks],
            ok=p.ok,
        )
        if p.contact is not None:
            result["contact"] = {"method": "compression_only_plane", **asdict(p.contact)}
    return out


def make_up_text(make_up: list[dict]) -> str:
    """The make-up of an RSN as the RPZ writes it: 1·L1 + 0,85·L2 − 0,5·L3."""
    text = ""
    for m in make_up:
        k = m["multiplier"]
        number = f"{abs(k):g}".replace(".", ",") + f"·L{m['case']}"
        if not text:
            text = ("−" if k < 0 else "") + number
        else:
            text += (" − " if k < 0 else " + ") + number
    return text


def write_facts(facts: dict[str, Any], out_dir: Path) -> str:
    """A Word file with the tables for sections 1.7 and 4 of the RPZ."""
    from docx import Document

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph("Загружения расчётной схемы")
    doc.add_paragraph(
        "γf и доля длительной — значения таблицы РСН (DCL) ЛИРЫ на загружение. Какие "
        "множители ЛИРА применила в каждом РСН, показывает состав РСН ниже."
    )
    t = doc.add_table(rows=1, cols=5)
    t.style = "Table Grid"
    heads = ("№", "Наименование", "Вид", "γf по DCL", "Доля длительной по DCL")
    for cell, h in zip(t.rows[0].cells, heads, strict=True):
        cell.text = h
    for c in facts["load_cases"]:
        values = (c["number"], c["name"], c["kind"], c.get("gamma_f"), c.get("long_term_share"))
        for cell, v in zip(t.add_row().cells, values, strict=True):
            cell.text = (
                "" if v is None else f"{v:g}".replace(".", ",") if isinstance(v, float) else str(v)
            )
    if facts.get("combinations"):
        doc.add_paragraph("Расчётные сочетания нагрузок (РСН): множители к результатам загружений")
        t = doc.add_table(rows=1, cols=3)
        t.style = "Table Grid"
        for cell, h in zip(t.rows[0].cells, ("№", "Наименование", "Состав"), strict=True):
            cell.text = h
        for c in facts["combinations"]:
            parts = c.get("warning") or make_up_text(c.get("make_up", []))
            if "warning" not in c and c.get("make_up") is not None and not c.get("exact"):
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


FACTS_SCHEMA = "lira-sapr-mcp/rpz-facts"
FACTS_VERSION = 1  # draft, docs/decisions/0006-lira-plus-exchange.md


def write_facts_json(facts: dict[str, Any], out_dir: Path, unsaved: bool = False) -> str:
    """The same facts for LIRA+ (draft exchange format 0006): the model file with its
    SHA-256 (upper case, as LiraRez.Hash), load cases, RSN and stiffnesses. ``unsaved``: the
    model open in LIRA has changes not in its file, so the hash is of another version."""
    import hashlib
    import json

    from lira_sapr_mcp import __version__

    out_dir.mkdir(parents=True, exist_ok=True)
    model = Path(facts["model"])
    sha = None
    if model.is_file():
        with model.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest().upper()
    data = {
        "schema": FACTS_SCHEMA,
        "version": FACTS_VERSION,
        "generator": f"lira-sapr-mcp {__version__}",
        "model": {"path": str(model), "sha256": sha}
        | ({"unsaved_changes": True} if unsaved else {}),
        "load_cases": facts["load_cases"],
        "combinations": facts.get("combinations", []),
        "stiffnesses": facts["stiffnesses"],
    }
    if "dynamic_cases_left_out" in facts:
        data["dynamic_cases_left_out"] = facts["dynamic_cases_left_out"]
    path = out_dir / f"{model.stem}_rpz_facts.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
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
