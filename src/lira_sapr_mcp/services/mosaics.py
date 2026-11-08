"""Reinforcement mosaics of the slabs of a LIRA model: the areas LIRA selected (its
envelope over the design combinations), sorted into the bands of a scale, drawn per
slab and component (PNG), outlined as zones for drawings (DXF) and reported."""

import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing import plots
from lira_sapr_mcp.postprocessing.dxf import Plan, Zone, write_zones
from lira_sapr_mcp.postprocessing.mesh import LEVEL_TOLERANCE, Slab, outline_loops
from lira_sapr_mcp.postprocessing.mosaic import (
    COMPONENTS,
    NO_DESIGN,
    Bars,
    Scale,
    addon_scale,
    step_scale,
    zone_areas,
)
from lira_sapr_mcp.postprocessing.report import Report, SlabPart
from lira_sapr_mcp.services.slabs import (
    Note,
    files_name,
    level_text,
    open_model,
    part_fields,
    part_line,
    read_mesh,
    ru,
    slab_file,
    slab_title,
    slabs_of,
    write_reports,
)


def read_reinforcement(
    adapter: Any,
    model: str | None,
    within: list[int] | None,
    components: list[str],
    design_option: int = 1,
    tolerance: float = LEVEL_TOLERANCE,
) -> tuple[str, list[Slab], dict[str, list[np.ndarray]], list[Note]]:
    """On the COM thread: the model's path, its slabs, per component the area (cm2/m) of each
    slab element (NO_DESIGN where LIRA designed nothing, e.g. no design data; NaN where it could
    not design the section), and notes about what was not taken as a support: the drawings mark
    the supports."""
    path = open_model(adapter, model)
    mesh = read_mesh(adapter, path)
    slabs = slabs_of(mesh, within, tolerance)
    elements = sorted(int(e) for s in slabs for e in s.elements)
    data = adapter.plate_reinforcement(path, elements, components, design_option)
    if not any(data.values()):
        raise LiraInputError(
            "LIRA selected no reinforcement for these slabs: set reinforced concrete design "
            "data (materials of the plates) and run lira_run_analysis"
        )
    values = {
        c: [np.array([data[c].get(int(e), NO_DESIGN) for e in s.elements]) for s in slabs]
        for c in components
    }
    return path, slabs, values, mesh.notes


def scale_of(
    levels: list[float] | None,
    background: dict | None,
    addons: list[dict] | None,
    values: dict[str, list[np.ndarray]],
) -> Scale:
    """The scale the agent asked for, or steps with a round step over all values."""
    try:
        if background or addons:
            if not (background and addons):
                raise ValueError("give both background and addons")
            return addon_scale(_bars(background), [_bars(a) for a in addons])
        if levels:
            return step_scale(levels)
    except ValueError as exc:
        raise LiraInputError(str(exc)) from exc
    top = max(
        (float(np.nanmax(v)) for vs in values.values() for v in vs if np.isfinite(v).any()),
        default=1.0,
    )
    return step_scale(plots.nice_levels(0.0, max(top, 0.1), 8).tolist())


def summaries(
    slabs: list[Slab], values: dict[str, list[np.ndarray]], scale: Scale
) -> list[dict[str, Any]]:
    """Per slab and component: the largest area, where, and the plan area of each band."""
    out = []
    for k, slab in enumerate(slabs):
        entry: dict[str, Any] = {
            "slab": slab.number,
            "z_m": round(slab.z, 3),
            "element_count": len(slab.elements),
            **part_fields(slab),
            "components": {},
        }
        for c, vs in values.items():
            v = vs[k]
            band_of = scale.bands(v)
            areas = zone_areas(slab, band_of, len(scale.labels))
            finite = np.where(np.isfinite(v) & (v >= 0), v, -np.inf)
            i = int(np.argmax(finite))
            entry["components"][c] = {
                "max_cm2_per_m": round(float(finite[i]), 2) if np.isfinite(finite[i]) else None,
                "max_element": int(slab.elements[i]),
                "not_designed": int(np.isnan(v).sum()),
                "no_design_data": int((v == NO_DESIGN).sum()),
                "zones_m2": {scale.labels[b]: round(a, 2) for b, a in enumerate(areas) if a > 0},
            }
        out.append(entry)
    return out


def mosaic_files(
    model_path: str,
    slabs: list[Slab],
    values: dict[str, list[np.ndarray]],
    scale: Scale,
    found: list[dict[str, Any]],
    out_dir: Path,
    formats: list[str],
    report: str | None,
    title: str | None,
    design_option: int = 1,
    notes: list[str] | None = None,
    chosen: bool = False,
) -> dict[str, Any]:
    """A DXF of zones per slab with a plan per component, the report, and on request a PNG
    per slab and component (the report's pictures are otherwise left out of ``out_dir``)."""
    stem = Path(model_path).stem
    files: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as temp:
        where = out_dir if "png" in formats else Path(temp)
        pictures: dict[int, list[Path]] = {}
        for k, slab in enumerate(slabs):
            name = slab_title(slab.number, slab.z, slab.part)
            plans = []
            for c, vs in values.items():
                band_of = scale.bands(vs[k])
                heading = f"{name}: {COMPONENTS.get(c, c)}, см²/м"
                if "png" in formats or report:
                    png = plots.mosaic(
                        slab, scale.colors, band_of, scale.labels,
                        where / f"{stem}_{slab_file(slab)}_{c}.png", heading, slab.site,
                    )  # fmt: skip
                    pictures.setdefault(slab.number, []).append(png)
                if "dxf" in formats:
                    plans.append(
                        Plan(
                            heading,
                            _zones(slab, c, band_of, scale),
                            f"{c}_VALUES",
                            _value_texts(vs[k]),
                            f"{c}: требуемая арматура в элементе, см²/м",
                        )
                    )
            if "dxf" in formats:
                dxf = write_zones(
                    out_dir / f"{stem}_{slab_file(slab)}_reinforcement.dxf",
                    slab, outline_loops(slab), plans,
                )  # fmt: skip
                files.setdefault("dxf", []).append(str(dxf))
        if "png" in formats:
            files["png"] = [str(p) for ps in pictures.values() for p in ps]
        if report:
            content = Report(
                title or f"Армирование плит — {stem}",
                [
                    f"Модель: {model_path}",
                    "Армирование, подобранное ЛИРОЙ по огибающей расчётных сочетаний "
                    f"(вариант конструирования {design_option}), см²/м.",
                    "Шкала: " + _scale_text(scale) + "; едина для всех плит и направлений.",
                ],
                ["Плита", "Отметка, м", "Арматура", "Наибольшая, см²/м", "Зоны (площадь, м²)"],
                [
                    [str(e["slab"]), level_text(e["z_m"]), c, _max_text(d), _zones_text(d)]
                    for e in found
                    for c, d in e["components"].items()
                ],
                [_part(e, pictures.get(e["slab"], [])) for e in found],
                notes or [],
            )
            report_name = f"{files_name(stem, slabs, chosen)}_reinforcement"
            files |= write_reports(content, out_dir / report_name, report)
    return files


def _zones(slab: Slab, component: str, band_of: np.ndarray, scale: Scale) -> list[Zone]:
    return [
        Zone(
            f"{component}_{scale.codes[b]}",
            scale.colors[b],
            f"{component}: {label}",
            outline_loops(slab, band_of == b),
        )
        for b, label in enumerate(scale.labels)
        if scale.drawn[b] and (band_of == b).any()
    ]


def _value_texts(values: np.ndarray) -> list[str | None]:
    """The area each element needs, as LIRA writes it; a dash where the section could not be
    designed, nothing where nothing was designed."""
    return [None if v == NO_DESIGN else "—" if np.isnan(v) else f"{v:.2f}" for v in values.tolist()]


def _bars(spec: dict) -> Bars:
    usage = 'bars: {"diameter": mm, "spacing": mm}, e.g. {"diameter": 12, "spacing": 200}'
    try:
        bars = Bars(float(spec["diameter"]), float(spec["spacing"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(usage) from exc
    if bars.diameter <= 0 or bars.spacing <= 0:
        raise ValueError(usage + "; both above zero")
    return bars


def _scale_text(scale: Scale) -> str:
    if scale.kind == "steps":
        return "ступени " + ", ".join(scale.labels[:-1])
    return f"фон {scale.background.label} и доборы " + ", ".join(a.label for a in scale.addons)


def _max_text(d: dict[str, Any]) -> str:
    if d["max_cm2_per_m"] is None:
        return "—"
    return f"{ru(d['max_cm2_per_m'])} (эл. {d['max_element']})"


def _zones_text(d: dict[str, Any]) -> str:
    return "; ".join(f"{label}: {ru(a)}" for label, a in d["zones_m2"].items()) or "—"


def _part(entry: dict[str, Any], pictures: list[Path]) -> SlabPart:
    title = slab_title(entry["slab"], entry["z_m"], bool(entry.get("part_of_slab")))
    part = SlabPart(title, pictures=pictures)
    part.lines += part_line(entry, entry["element_count"])
    for c, d in entry["components"].items():
        line = f"{COMPONENTS.get(c, c)}: наибольшая {_max_text(d)} см²/м; зоны: {_zones_text(d)}."
        if d["not_designed"]:
            line += f" Не подобрано сечение у {d['not_designed']} элементов."
        part.lines.append(line)
    return part
