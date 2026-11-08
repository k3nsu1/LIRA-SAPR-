"""Turn raw result rows into small answers: units, selection, extremes, top N,
envelopes, thresholds and full tables written to CSV or Excel."""

from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraInputError

TF_TO = {
    "t": 1.0,
    "kg": 1000.0,
    "g": 1e6,
    "N": 9806.65,
    "kN": 9.80665,
    "MN": 0.00980665,
    "lb": 2204.62262,
    "kips": 2.20462262,
}
M_TO = {"m": 1.0, "cm": 100.0, "mm": 1000.0, "ft": 3.28083990, "in": 39.3700787}

DISPLACEMENT_KIND = {
    "X": "length",
    "Y": "length",
    "Z": "length",
    "UX": "angle",
    "UY": "angle",
    "UZ": "angle",
}


# 1 tf/m2 in pressure units, for stresses set in LIRA as Pa, kPa or MPa.
TFM2_TO = {"Pa": 9806.65, "kPa": 9.80665, "MPa": 0.00980665}


def _known(value: str | None, table: dict, default: str) -> str:
    # A unit LIRA reports that we do not know (e.g. "none") falls back to LIRA's own.
    return value if value in table else default


def convert(rows: list[dict], kinds: dict[str, str], units: dict[str, str]) -> dict[str, str]:
    """Convert LIRA's result units (m, rad, tf and products) in place; return the units.

    units: "length" (displacements), "force" and "moment_length" (forces), "stress" and
    "stress_length" (stresses), as set in LIRA.
    """
    length = _known(units.get("length"), M_TO, "m")
    force = _known(units.get("force"), TF_TO, "t")
    per = _known(units.get("moment_length"), M_TO, "m")
    f, lf = TF_TO[force], M_TO[per]
    factors = {
        "length": (M_TO[length], length),
        "angle": (1.0, "rad"),
        "force": (f, force),
        "moment": (f * lf, f"{force}*{per}"),
        "bimoment": (f * lf * lf, f"{force}*{per}2"),
        "force_per_length": (f / lf, f"{force}/{per}"),
        "moment_per_length": (f, f"{force}*{per}/{per}"),
    }
    stress = units.get("stress")
    if stress in TFM2_TO:
        factors["stress"] = (TFM2_TO[stress], stress)
    else:
        sf = _known(stress, TF_TO, "t")
        sl = _known(units.get("stress_length"), M_TO, "m")
        factors["stress"] = (TF_TO[sf] / M_TO[sl] ** 2, f"{sf}/{sl}2")
    for row in rows:
        for comp, kind in kinds.items():
            if comp in row:
                row[comp] = row[comp] * factors[kind][0]
    return {comp: factors[kind][1] for comp, kind in kinds.items()}


def summarize(
    rows: list[dict],
    components: list[str],
    *,
    sort_by: str | None = None,
    by_abs: bool = True,
    top: int | None = 20,
    max_rows: int = 500,
    keep: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Extremes per component and the rows the agent asked for, never thousands of them.

    keep: more keys to show in rows (e.g. the envelope's ``<c>_min`` and ``<c>_max_at``).
    """
    if top is not None and top < 1:
        raise LiraInputError("top must be at least 1 (or null for up to 500 rows)")
    present = [c for c in components if any(c in r for r in rows)]
    unknown = sorted(set(components) - set(present))
    extremes = {}
    for comp in present:
        vals = [r for r in rows if comp in r]
        # Envelope rows keep both ends: the minimum from <c>_min, the maximum from <c>_max.
        lo_key = f"{comp}_min" if all(f"{comp}_min" in r for r in vals) else comp
        hi_key = f"{comp}_max" if all(f"{comp}_max" in r for r in vals) else comp
        lo = min(vals, key=lambda r: r[lo_key])
        hi = max(vals, key=lambda r: r[hi_key])
        extremes[comp] = {
            "min": {"value": lo[lo_key], "at": _where(lo, lo_key)},
            "max": {"value": hi[hi_key], "at": _where(hi, hi_key)},
        }
    selected = rows
    if sort_by:
        if sort_by not in present:
            raise LiraInputError(f"Cannot sort by {sort_by!r}; components: {present}")
        selected = sorted(
            (r for r in rows if sort_by in r),
            key=lambda r: abs(r[sort_by]) if by_abs else r[sort_by],
            reverse=True,
        )
    limit = min(top, max_rows) if top else max_rows
    shown = [
        {k: _round(v) for k, v in r.items() if k in present or k in LOCATION_KEYS or k in keep}
        for r in selected[:limit]
    ]
    result: dict[str, Any] = {
        "total_rows": len(rows),
        "extremes": {
            c: {k: {"value": _round(v["value"]), "at": v["at"]} for k, v in e.items()}
            for c, e in extremes.items()
        },
        "rows": shown,
        "rows_shown": len(shown),
    }
    if unknown:
        result["components_not_available"] = unknown
    if len(selected) > len(shown):
        result["note"] = f"{len(selected) - len(shown)} more rows not shown: narrow the request"
    return result


# Where a value is: kept in every row shown and in the place of each extreme.
LOCATION_KEYS = (
    "node",
    "element",
    "case",
    "combination",
    "shape",
    "section",
    "symmetric",
    "limit_state",
    "dcf",
    "criterion",
    "group",
    "load_cases",
    "with",
)


# Keys that tell one result of a place from another; an envelope runs over them.
VARIANT_KEYS = ("case", "combination", "shape", "limit_state", "dcf", "criterion", "group",
                "load_cases", "with")  # fmt: skip


def reduce_tables(
    tables: dict[str, list[dict]],
    units_of: dict[str, dict[str, str]],
    *,
    components: list[str] | None,
    sort_by: str | None,
    by_abs: bool,
    top: int | None,
    at_least: float | None,
    with_envelope: bool,
) -> tuple[dict[str, Any], dict[str, list[dict]], dict[str, dict[str, str]]]:
    """Per table: envelope, then threshold, then extremes and top rows.

    Returns the answers, the full tables to export and the units of their columns.
    """
    if at_least is not None and not sort_by:
        raise LiraInputError("threshold needs sort_by: the component to compare")
    answers: dict[str, Any] = {}
    full: dict[str, list[dict]] = {}
    full_units: dict[str, dict[str, str]] = {}
    for name, rows in tables.items():
        units = dict(units_of[name])
        wanted = components or [c for c in units if any(c in r for r in rows)]
        keep: tuple[str, ...] = ()
        if with_envelope:
            rows = envelope(rows, wanted)
            keep = tuple(f"{c}_{e}" for c in wanted for e in ("min", "max", "min_at", "max_at"))
            for c in wanted:
                if c in units:
                    units[f"{c}_min"] = units[f"{c}_max"] = units[c]
        has_sort = bool(sort_by) and any(sort_by in r for r in rows)
        if at_least is not None and has_sort:
            rows = threshold(rows, sort_by, at_least, by_abs)
        summary = summarize(
            rows, wanted, sort_by=sort_by if has_sort else None, by_abs=by_abs, top=top, keep=keep
        )
        if sort_by and not has_sort:
            summary["note"] = f"no {sort_by} in {name}: rows are not sorted or filtered"
        summary["units"] = {c: units[c] for c in wanted if c in units}
        answers[name], full[name], full_units[name] = summary, rows, units
    return answers, full, full_units


def threshold(rows: list[dict], component: str, value: float, by_abs: bool = True) -> list[dict]:
    """Rows whose component reaches the threshold (by absolute value unless by_abs=False)."""
    if by_abs:
        return [r for r in rows if component in r and abs(r[component]) >= value]
    return [r for r in rows if component in r and r[component] >= value]


def envelope(rows: list[dict], components: list[str]) -> list[dict]:
    """Per place (node, or element and section): minimum and maximum of each component over
    load cases, combinations, shapes and limit states, with where each one occurs.

    Rows get ``<c>_min``, ``<c>_max`` and ``<c>`` = the one of them larger in magnitude,
    so they can be sorted and summarized like plain result rows.
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        place = tuple((k, row[k]) for k in LOCATION_KEYS if k in row and k not in VARIANT_KEYS)
        env = groups.setdefault(place, dict(place))
        variant = {k: row[k] for k in ("case", "combination", "shape", "limit_state", "dcf")
                   if k in row}  # fmt: skip
        for c in components:
            if c not in row:
                continue
            v = row[c]
            if f"{c}_min" not in env or v < env[f"{c}_min"]:
                env[f"{c}_min"], env[f"{c}_min_at"] = v, variant
            if f"{c}_max" not in env or v > env[f"{c}_max"]:
                env[f"{c}_max"], env[f"{c}_max_at"] = v, variant
    out = []
    for env in groups.values():
        for c in components:
            if f"{c}_min" in env:
                lo, hi = env[f"{c}_min"], env[f"{c}_max"]
                env[c] = lo if abs(lo) > abs(hi) else hi
        out.append(env)
    return out


def export(
    tables: dict[str, list[dict]], units_of: dict[str, dict[str, str]], path: str
) -> dict[str, Any]:
    """Write full tables to .csv (one file; a "family" column when several tables) or .xlsx
    (a sheet per table). Column headers carry each table's own units, e.g. "My, kN*m", so
    same-named components of different units stay in separate columns."""
    import pandas as pd

    target = Path(path)
    if not target.is_absolute():
        raise LiraInputError(f"export_path must be an absolute path, got {path!r}")
    suffix = target.suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise LiraInputError("export_path must end with .csv or .xlsx")

    def frame(name: str) -> Any:
        units = units_of.get(name, {})
        df = pd.DataFrame([{k: _cell(v) for k, v in r.items()} for r in tables[name]])
        return df.rename(columns={c: f"{c}, {units[c]}" for c in df.columns if c in units})

    replaced = target.exists()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".csv":
            frames = [frame(n).assign(**({"family": n} if len(tables) > 1 else {}))
                      for n in tables]  # fmt: skip
            data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            data.to_csv(target, index=False, encoding="utf-8-sig")
        else:
            with pd.ExcelWriter(target, engine="openpyxl") as writer:
                for n in tables or {"results": []}:
                    df = frame(n) if n in tables else pd.DataFrame()
                    df.to_excel(writer, sheet_name=n[:31] or "results", index=False)
    except (OSError, ValueError) as exc:
        raise LiraInputError(f"Could not write {target}: {exc}") from exc
    rows = sum(len(r) for r in tables.values())
    return {"path": str(target), "rows": rows, "replaced_existing_file": replaced}


def _cell(v: Any) -> Any:
    # Nested values (where an envelope extreme occurs, design errors) as compact text.
    if isinstance(v, dict):
        return ", ".join(f"{k}={x}" for k, x in v.items())
    return v


def _where(row: dict, key: str = "") -> dict:
    where = {k: row[k] for k in LOCATION_KEYS if k in row}
    variant = row.get(f"{key}_at")  # an envelope row: where its min or max occurs
    return where | variant if isinstance(variant, dict) else where


def _round(v: Any) -> Any:
    return float(f"{v:.6g}") if isinstance(v, float) else v
