"""Diagrams of forces in bars (N, Qz, My, Mz ...) for the RPZ appendices: one component on
one view of the frame, PNG A4 landscape, 200 dpi. The ordinates are drawn in the picture
plane across each bar, from the values LIRA gives in the bar's design sections."""

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np
from matplotlib.collections import LineCollection, PolyCollection

from lira_sapr_mcp.postprocessing.frame_plots import GREY, figure, project, save

SHARE = 0.08  # the largest ordinate, of the drawing's size
POSITIVE, NEGATIVE = "#d62728", "#1f77b4"
FLAT = 1e-9  # m: a bar shorter than this in the picture is seen end on


@dataclass
class Extreme:
    value: float
    element: int
    section: int


def stations(count: int) -> list[float]:
    """Where the design sections of a bar lie, as parts of its length from its first node:
    evenly, the first at the start and the last at the end."""
    if count < 2:
        raise ValueError("a bar has at least two design sections")
    return [k / (count - 1) for k in range(count)]


def side(a: np.ndarray, b: np.ndarray) -> np.ndarray | None:
    """The unit normal in the picture along which positive ordinates of the bar from ``a`` to
    ``b`` (projected points) are drawn: up for a bar that is not vertical in the picture,
    left for a vertical one, whichever way the bar is numbered. None: the bar is seen end on."""
    d = b - a
    length = float(np.hypot(*d))
    if length < FLAT:
        return None
    if d[0] < -FLAT * length or (abs(d[0]) <= FLAT * length and d[1] < 0):
        d = -d
    return np.array([-d[1], d[0]]) / length


def _pieces(ts: list[float], vs: list[float]) -> list[tuple[float, float, float, float]]:
    """Consecutive (t1, v1, t2, v2) between sections, split where the value changes sign so
    that each piece is on one side of the bar."""
    out = []
    for (t1, v1), (t2, v2) in pairwise(zip(ts, vs, strict=True)):
        if v1 * v2 < 0:
            tz = t1 + (t2 - t1) * v1 / (v1 - v2)
            out += [(t1, v1, tz, 0.0), (tz, 0.0, t2, v2)]
        else:
            out.append((t1, v1, t2, v2))
    return out


def extremes(values: dict[int, list[float]]) -> tuple[Extreme | None, Extreme | None]:
    """The largest and the smallest value and where they are (element, section from 1); on
    a tie the smaller element number and section."""
    flat = [(v, el, s) for el in sorted(values) for s, v in enumerate(values[el], start=1)]
    if not flat:
        return None, None
    hi = max(flat, key=lambda x: (x[0], -x[1], -x[2]))
    lo = min(flat, key=lambda x: (x[0], x[1], x[2]))
    return Extreme(*hi), Extreme(*lo)


def label(name: str, value: float, unit: str) -> str:
    text = f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"
    return f"{name} = {text} {unit}".replace(".", ",").replace("-", "−")


def diagram(
    nodes: dict[int, tuple[float, float, float]],
    bars: dict[int, tuple[int, int]],
    values: dict[int, list[float]],
    name: str,
    unit: str,
    view: str,
    path: Path,
    title: str,
) -> tuple[Path, float, list[int]]:
    """The bars in grey and, across each bar with values, its diagram: positive ordinates red
    and up (left on a vertical bar), negative blue on the other side, all at one scale (the
    largest is SHARE of the drawing). The largest and the smallest value are signed with
    their element. Returns the file, the scale (m of drawing per unit of value) and the bars
    seen end on in this view, which have no diagram."""
    fig, ax = figure(title)
    keys = sorted(bars)
    ends = {el: project(np.array([nodes[a] for a in bars[el]]), view) for el in keys}
    every = np.vstack(list(ends.values())) if ends else np.zeros((1, 2))
    size = float(np.max(np.ptp(every, axis=0))) or 1.0
    ax.add_collection(LineCollection([ends[el] for el in keys], colors=GREY, linewidths=0.8))
    top = max((abs(v) for vs in values.values() for v in vs), default=0.0)
    scale = SHARE * size / top if top > 0 else 0.0
    polys, colours, outline, end_on = [], [], [], []
    for el in keys:
        if el not in values:
            continue
        a, b = ends[el]
        normal = side(a, b)
        if normal is None:
            end_on.append(el)
            continue
        vs = values[el]
        ts = stations(len(vs))
        for t1, v1, t2, v2 in _pieces(ts, vs):
            p1, p2 = a + t1 * (b - a), a + t2 * (b - a)
            q1, q2 = p1 + scale * v1 * normal, p2 + scale * v2 * normal
            polys.append([p1, p2, q2, q1])
            colours.append(POSITIVE if v1 + v2 > 0 else NEGATIVE)
        tips = [a + t * (b - a) + scale * v * normal for t, v in zip(ts, vs, strict=True)]
        outline.append([a, *tips, b])
    ax.add_collection(PolyCollection(polys, facecolors=colours, edgecolors="none", alpha=0.35))
    ax.add_collection(LineCollection(outline, colors="#333333", linewidths=0.7))
    hi, lo = extremes({el: values[el] for el in values if el in bars and el not in end_on})
    marks = [e for e in (hi, lo) if e is not None and e.value != 0]
    if hi and lo and hi.element == lo.element and hi.section == lo.section:
        marks = marks[:1]
    for k, e in enumerate(marks):
        a, b = ends[e.element]
        t = stations(len(values[e.element]))[e.section - 1]
        at = a + t * (b - a) + scale * e.value * side(a, b)
        ax.annotate(
            f"{label(name, e.value, unit)} (эл. {e.element})",
            at,
            fontsize=9,
            xytext=(6, 6 + 12 * k),
            textcoords="offset points",
        )
    ax.plot([], [], color=POSITIVE, linewidth=6, alpha=0.35, label=f"{name} > 0")
    ax.plot([], [], color=NEGATIVE, linewidth=6, alpha=0.35, label=f"{name} < 0")
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1.0, 1.0),
        fontsize=8,
        frameon=False,
        title=f"{unit}; знак — как в ЛИРЕ",
    )
    ax.autoscale()
    return save(fig, path), scale, end_on
