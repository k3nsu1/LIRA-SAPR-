"""DXF of zones for the engineer's drawing (ezdxf): reinforcement, deflection, settlement.

Millimetres. One plan per reinforcement component, two in a row: the first stays at the
model's plan coordinates, the others are moved aside whole. Every component and band is a
layer of its own (``AS1_add_D16s200``) holding closed polylines of the zone outlines and a
hatch in the band's colour, so a drawing shows or hides each one; the area each element
needs is written at its centre, in a layer of its own per component (``AS1_VALUES``); the
slab outline and its supports are layers too, and a legend beside each plan lists its
layers in words.
"""

from dataclasses import dataclass
from pathlib import Path

import ezdxf
import numpy as np
from ezdxf.enums import TextEntityAlignment

from lira_sapr_mcp.postprocessing.mesh import Slab, outline_loops

MM = 1000.0
TEXT = 250.0  # mm: height of legend text
TITLE = 1.2 * TEXT  # mm: height of a plan's title
ROW = 2 * TEXT  # mm: from one legend line to the next
GAP = 2000.0  # mm: between a plan with its legend and the next one
VALUE_HEIGHT = 0.22  # of an element's side: five characters fit into the element


@dataclass(frozen=True)
class Zone:
    layer: str
    color: str  # "#rrggbb"
    text: str  # for the legend
    loops: list[np.ndarray]  # outlines, m


@dataclass(frozen=True)
class Plan:
    """One component's plan: its title, its zones and per slab element the text to write at
    its centre (None: nothing), in the layer ``value_layer``."""

    title: str
    zones: list[Zone]
    value_layer: str = ""
    values: list[str | None] = ()
    values_text: str = ""  # for the legend


def field_plan(
    slab: Slab,
    node_values: np.ndarray,
    levels: np.ndarray,
    colors: list[str],
    title: str,
    prefix: str,
    what: str,
    unit: str = "",
) -> Plan:
    """A mosaic of a field known at the nodes: each element has the mean of its nodes, written
    at its centre, and lies in the zone of its band. ``colors``: one per band between ``levels``,
    or with one more for the band above the last level. A node without a value (NaN) leaves its
    elements out."""
    values = np.array([node_values[f].mean() for f in slab.faces])
    band_of = np.searchsorted(levels, values, side="right") - 1
    band_of = np.clip(band_of, 0, len(colors) - 1)
    band_of[np.isnan(values)] = -1
    between = len(levels) - 1
    tail = f" {unit}" if unit else ""
    zones = []
    for b, color in enumerate(colors):
        if (band_of == b).any():
            a = float(levels[b])
            band, code = (
                (
                    f"{_ru(a)}…{_ru(float(levels[b + 1]))}",
                    f"{_code(a)}_{_code(float(levels[b + 1]))}",
                )
                if b < between
                else (f"больше {_ru(a)}", f"over{_code(a)}")
            )
            zones.append(
                Zone(f"{prefix}_{code}", color, f"{what}: {band}{tail}",
                     outline_loops(slab, band_of == b))
            )  # fmt: skip
    return Plan(
        title,
        zones,
        f"{prefix}_VALUES",
        [None if np.isnan(v) else f"{v:.2f}" for v in values.tolist()],
        f"{what} в элементе: среднее по его узлам{',' + tail if tail else ''}",
    )


def _ru(x: float) -> str:
    return f"{x:g}".replace("-", "−").replace(".", ",")


def _code(x: float) -> str:
    return f"{x:g}".replace("-", "m").replace(".", "p")


def write_zones(path: Path, slab: Slab, outline: list[np.ndarray], plans: list[Plan]) -> Path:
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    doc.styles.add("RU", font="arial.ttf")
    msp = doc.modelspace()
    for layer in ("SLAB", "SUPPORTS", "LEGEND"):
        _layer(doc, layer, "#000000")
    width, height = np.ptp(slab.xy, axis=0) * MM
    lines = max(len(p.zones) for p in plans) + 1
    longest = max(
        (len(s) for p in plans for s in (*map(_legend, p.zones), _values_legend(p)) if s),
        default=30,
    )
    # 0.7: the width of an average letter to its height, with room for a wider font than
    # Arial where the drawing program has no Arial. A small slab's title may be wider than
    # the plan with its legend.
    legend = width + 1000 + 800 + longest * TEXT * 0.7
    title = max(len(p.title) for p in plans) * TITLE * 0.7
    step_x = max(legend, title) + GAP
    step_y = max(height, ROW * (lines + 1)) + 3 * TEXT + GAP
    sides = np.linalg.norm(
        np.concatenate([slab.xy[f] - slab.xy[np.roll(f, -1)] for f in slab.faces]), axis=1
    )
    value_height = VALUE_HEIGHT * float(np.median(sides)) * MM
    centres = np.array([slab.xy[f].mean(axis=0) for f in slab.faces]) * MM
    for k, plan in enumerate(plans):
        shift = np.array([(k % 2) * step_x, -(k // 2) * step_y])
        _plan(doc, msp, slab, outline, plan, shift, centres, value_height)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


def _plan(
    doc,
    msp,
    slab: Slab,
    outline: list[np.ndarray],
    plan: Plan,
    shift: np.ndarray,
    centres: np.ndarray,
    value_height: float,
) -> None:
    for loop in outline:
        msp.add_lwpolyline(loop * MM + shift, close=True, dxfattribs={"layer": "SLAB"})
    if not slab.springs_all_over:  # a support at nearly every node says nothing on a drawing
        for i in slab.supports:
            msp.add_circle(slab.xy[i] * MM + shift, 100, dxfattribs={"layer": "SUPPORTS"})
    for zone in plan.zones:
        _layer(doc, zone.layer, zone.color)
        hatch = msp.add_hatch(dxfattribs={"layer": zone.layer})
        hatch.rgb = _rgb(zone.color)
        for loop in zone.loops:
            moved = loop * MM + shift
            msp.add_lwpolyline(moved, close=True, dxfattribs={"layer": zone.layer})
            hatch.paths.add_polyline_path(moved, is_closed=True)
    if plan.value_layer:
        _layer(doc, plan.value_layer, "#000000")
        written = {"layer": plan.value_layer, "style": "RU"}
        for text, centre in zip(plan.values, centres + shift, strict=True):
            if text is not None:
                msp.add_text(text, height=value_height, dxfattribs=written).set_placement(
                    centre, align=TextEntityAlignment.MIDDLE_CENTER
                )
    low, high = slab.xy.min(axis=0) * MM + shift, slab.xy.max(axis=0) * MM + shift
    msp.add_text(
        plan.title, height=TITLE, dxfattribs={"layer": "LEGEND", "style": "RU"}
    ).set_placement((low[0], high[1] + TEXT))
    x0, y = high[0] + 1000, high[1]
    for zone in plan.zones:
        if not zone.loops:
            continue
        y -= ROW
        box = [(x0, y), (x0 + 600, y), (x0 + 600, y + 400), (x0, y + 400)]
        patch = msp.add_hatch(dxfattribs={"layer": "LEGEND"})
        patch.rgb = _rgb(zone.color)
        patch.paths.add_polyline_path(box, is_closed=True)
        msp.add_text(
            _legend(zone), height=TEXT, dxfattribs={"layer": "LEGEND", "style": "RU"}
        ).set_placement((x0 + 800, y))
    if plan.value_layer:
        msp.add_text(
            _values_legend(plan), height=TEXT, dxfattribs={"layer": "LEGEND", "style": "RU"}
        ).set_placement((x0 + 800, y - ROW))


def _values_legend(plan: Plan) -> str:
    return f"{plan.values_text} — слой {plan.value_layer}" if plan.value_layer else ""


def _legend(zone: Zone) -> str:
    return f"{zone.text} — слой {zone.layer}"


def _layer(doc: ezdxf.document.Drawing, name: str, color: str) -> None:
    if name not in doc.layers:
        doc.layers.add(name, true_color=ezdxf.rgb2int(_rgb(color)))


def _rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
