"""Service: the pictures of the RPZ appendices as LIRA draws them, laid out like the
owner's sample explanatory note: per model the design scheme (appendix В), and per group
of elements its mosaics of forces by the envelopes, the steel check and the displacements
(appendices Ж, И, К), and for a foundation the soil pressure Rz and the settlement (Л).
LIRA's window is set for each picture (adapters/result_view.py) and captured; nothing is
drawn or recalculated by the server. LIRA's own display settings (the scale, loads drawn,
values on elements, design sections) are the user's and stay as they are."""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lira_sapr_mcp.adapters.result_view import (
    ENVELOPES,
    MOSAICS,
    STEEL,
    STEEL_SECTION_CHECKS,
)
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services.slabs import open_model

# The pictures of a group of steel elements in the sample RPZ (the arch, appendix Ж) and of a
# foundation (appendix Л).
STEEL_GROUP = ("N:MAXIMUM", "N:MINIMUM", "steel_1LS", "steel_LB", "disp_Z:rsn")
FOUNDATION = ("Rz:MINIMUM", "Rz:MAXIMUM", "Rz:rsn", "disp_Z:rsn")
SCHEME_VIEWS = ("isometric", "xoy", "xoz")
VIEWS = ("isometric", "xoy", "xoz", "current")
WORDS = {
    "N": "N",
    "Qy": "Qy",
    "Qz": "Qz",
    "My": "My",
    "Mz": "Mz",
    "Mx": "Mx",
    "disp_X": "перемещения X",
    "disp_Y": "перемещения Y",
    "disp_Z": "перемещения Z",
    "Rz": "напряжения Rz",
    "steel_1LS": "сталь 1ПС",
    "steel_2LS": "сталь 2ПС",
    "steel_LB": "сталь местная устойчивость",
    "steel_max": "сталь огибающая",
    "steel_sel_1LS": "сталь подбор 1ПС",
    "steel_sel_2LS": "сталь подбор 2ПС",
    "steel_sel_LB": "сталь подбор местная устойчивость",
    "steel_sel_max": "сталь подбор огибающая",
}
for _key, (_, _words) in STEEL_SECTION_CHECKS.items():
    WORDS["steel_sec_" + _key] = "сталь по " + _words
    WORDS["steel_sel_sec_" + _key] = "сталь подбор по " + _words
ITEM_WORDS = {"MAXIMUM": "огибающая max", "MINIMUM": "огибающая min", "MAX ABS": "max abs"}


@dataclass
class Picture:
    appendix: str
    model: str
    heading: str  # the text above the picture in the RPZ (the group's title)
    view: str
    results: str | None = None  # "rsn", "load_cases" or None for the scheme
    mosaic: str | None = None
    item: int | str | None = None  # number, envelope, "rsn" (worst |Z| in the group)
    group: dict[str, Any] | None = None  # how the group's elements are given
    elements: list[int] | None = None
    file: str = ""
    shown: str = ""
    notes: list[str] = field(default_factory=list)


def parse_spec(spec: str) -> tuple[str, str | None, int | str | None]:
    """'N:MAXIMUM' -> ("N", "rsn", "MAXIMUM"); 'disp_Z:rsn' -> worst RSN of the group;
    'My:rsn5' -> RSN 5; 'Qz:case3' -> load case 3; 'steel_1LS' -> the steel check."""
    mosaic, _, item = spec.partition(":")
    if mosaic not in MOSAICS:
        raise LiraInputError(f"Unknown picture {spec!r}: mosaics are {', '.join(MOSAICS)}")
    if mosaic in STEEL:
        if item:
            raise LiraInputError(f"{spec!r}: the steel check takes no load case or RSN")
        # The steel check is LIRA's own envelope: any results mode shows it, and every
        # analysed model has load cases, not every one RSN (example 12).
        return mosaic, "load_cases", None
    if item in ENVELOPES:
        return mosaic, "rsn", item
    if item == "rsn":
        return mosaic, "rsn", "rsn"
    m = re.fullmatch(r"(rsn|case)(\d+)", item)
    if not m:
        raise LiraInputError(
            f"{spec!r}: after ':' one of {', '.join(ENVELOPES)}, rsn, rsn<N> or case<N>"
        )
    return mosaic, "rsn" if m.group(1) == "rsn" else "load_cases", int(m.group(2))


def _view(value: Any, where: str) -> str:
    view = value or "isometric"
    if view not in VIEWS:
        raise LiraInputError(f"{where}: view one of {', '.join(VIEWS)}")
    return view


def plan(models: list[dict[str, Any]]) -> list[Picture]:
    """The pictures, in the order of the RPZ, from the owner's description of the models:
    [{"model": path, "title": "Здание АБК", "scheme_appendix": "В", "appendix": "И",
      "groups": [{"title": "Усилия в арке", "stiffnesses": [1, 2], "view": "xoz",
                  "pictures": [...], "foundation": false}]}]."""
    if not models:
        raise LiraInputError("models: describe at least one model")
    out: list[Picture] = []
    for k, m in enumerate(models, start=1):
        path = m.get("model")
        if not path:
            raise LiraInputError(f"models[{k}]: model (a .lir path or title) is required")
        title = str(m.get("title") or Path(str(path)).stem)
        scheme = m.get("scheme_appendix")
        if scheme:
            for view in m.get("scheme_views") or SCHEME_VIEWS:
                out.append(Picture(str(scheme), str(path), title, _view(view, title)))
        groups = m.get("groups") or []
        if groups and not m.get("appendix"):
            raise LiraInputError(f"{title}: appendix (the letter of its results) is required")
        for g in groups:
            heading = str(g.get("title") or "")
            if not heading:
                raise LiraInputError(f"{title}: every group needs a title")
            if not (g.get("stiffnesses") or g.get("elements") or g.get("whole_model")):
                raise LiraInputError(
                    f"{title} / {heading}: give stiffnesses, elements or whole_model=true"
                )
            default = FOUNDATION if g.get("foundation") else STEEL_GROUP
            for spec in g.get("pictures") or default:
                mosaic, results, item = parse_spec(str(spec))
                out.append(
                    Picture(
                        str(m["appendix"]),
                        str(path),
                        heading,
                        _view(g.get("view"), heading),
                        results,
                        mosaic,
                        item,
                        {k: g.get(k) for k in ("stiffnesses", "elements", "whole_model")},
                    )
                )
    return out


def elements_of(adapter: Any, path: str, group: dict[str, Any]) -> list[int] | None:
    """The elements of a group: given numbers, or those of the given stiffnesses
    (Elements_Stiffnesses); None for the whole model."""
    if group.get("whole_model"):
        return None
    chosen = set(int(e) for e in group.get("elements") or [])
    wanted = {int(s) for s in group.get("stiffnesses") or []}
    if wanted:
        for r in adapter.read_table(path, "Elements_Stiffnesses"):
            if len(r) > 1 and r[0] is not None and r[1] is not None and int(r[1]) in wanted:
                chosen.add(int(r[0]))
        if not chosen:
            raise LiraInputError(f"No elements of stiffnesses {sorted(wanted)} in {path}")
    return sorted(chosen)


def worst_rsn(adapter: Any, path: str, elements: list[int] | None) -> tuple[int, float]:
    """The RSN with the largest |Z| among the nodes of the elements (all nodes for the
    whole model), and that |Z| in mm: which RSN to show is a fact of the results."""
    nodes: set[int] | None = None
    if elements is not None:
        wanted = set(elements)
        nodes = set()
        for r in adapter.read_table(path, "Elements_TypeAndNodes"):
            if r and r[0] is not None and int(r[0]) in wanted and len(r) > 2:
                nodes.update(int(n) for n in re.findall(r"\d+", str(r[2])))
    rows = adapter.combination_displacements(path, sorted(nodes) if nodes else None, None)
    best: tuple[int, float] | None = None
    for r in rows:
        z = abs(float(r.get("Z") or 0.0))
        if best is None or z > best[1]:
            best = (int(r["combination"]), z)
    if best is None:
        raise LiraInputError(f"No displacements by RSN in {path}: run lira_run_analysis")
    return best[0], best[1] * 1000


def file_name(p: Picture, number: int) -> str:
    what = "схема" if p.mosaic is None else WORDS[p.mosaic]
    item = ""
    if isinstance(p.item, str) and p.item in ITEM_WORDS:
        item = " " + ITEM_WORDS[p.item]
    elif p.shown:
        item = " " + p.shown
    view = "" if p.mosaic else f" {p.view}"
    raw = f"{p.appendix}{number:02d} {p.heading} - {what}{item}{view}"
    return re.sub(r'[<>:"/\\|?*]+', "-", raw).strip()[:150] + ".png"


def take(adapter: Any, pictures: list[Picture]) -> list[Picture]:
    """On the COM thread: set LIRA's window for each picture and capture it. The pixels
    stay in memory until written (``write``)."""
    for p in pictures:
        path = open_model(adapter, p.model)
        p.model = path
        p.elements = None if p.group is None else elements_of(adapter, path, p.group)
        item = p.item
        if item == "rsn":
            number, z = worst_rsn(adapter, path, p.elements)
            item = number
            p.notes = [f"РСН {number}: наибольшее |Z| в группе {z:.1f} мм".replace(".", ",")]
        else:
            p.notes = []
        width, height, pixels, shown, drawn = adapter.result_picture(
            path, p.results, item, p.mosaic, p.elements, p.view
        )
        if not drawn:
            p.notes.append(
                "Мозаики на картинке не видно: ЛИРА не нарисовала её с трёх попыток, нет такого "
                "результата (например, проверки стали) или результат одного цвета. Картинка "
                "может показывать прежнюю мозаику: проверить и при необходимости снять вручную"
            )
        p.shown = shown
        p._captured = (width, height, pixels)  # type: ignore[attr-defined]
    return pictures


def write(pictures: list[Picture], out_dir: Path) -> list[str]:
    """PNG files numbered within each appendix, and pictures.json with what each shows."""
    from lira_sapr_mcp.adapters.picture import picture_png

    out_dir.mkdir(parents=True, exist_ok=True)
    count: dict[str, int] = {}
    files = []
    for p in pictures:
        count[p.appendix] = count.get(p.appendix, 0) + 1
        name = file_name(p, count[p.appendix])
        (out_dir / name).write_bytes(picture_png(*p._captured))  # type: ignore[attr-defined]
        p.file = name
        files.append(str(out_dir / name))
    index = [
        {k: v for k, v in asdict(p).items() if k not in ("elements", "group")} for p in pictures
    ]
    (out_dir / "pictures.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return files
