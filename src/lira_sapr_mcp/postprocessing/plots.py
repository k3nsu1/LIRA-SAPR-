"""Pictures of slabs in plan: isofields of values at nodes and mosaics of values per
element, PNG. Each slab is drawn on its own, as large as the picture allows; a small key
plan beside it shows where the slab lies among the others. Uses matplotlib's object
interface with the Agg canvas, so nothing global (no pyplot state, no window) and it runs
in any thread."""

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.tri import Triangulation

from lira_sapr_mcp.postprocessing.mesh import Slab

DPI = 150
PAGE_INCHES = (10.5, 7.1)  # A4 less its margins, a little less to leave room for the title
RATIO_LEVELS = np.round(np.arange(0, 1.01, 0.1), 2)  # f / allowed: 0 ... 1, then over
MARK_COLOR = "#d00000"
OVER_COLOR = "#b00000"  # values above the last level of a scale
PLACED_LABELS = 40  # above this many labels on a picture they are not moved clear of each other
# Where a label may stand around its point (points on the page), tried in this order.
LABEL_PLACES = [
    (dx * r, dy * r)
    for r in (10, 26, 44)
    for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1))
]


@dataclass(frozen=True)
class Mark:
    """A place to show on an isofield: a point (x, y) on it, its label and the line it
    belongs to (a span between supports, a cantilever, two points of a settlement
    difference). ``cross``: the point is a reading itself, marked with a cross."""

    point: tuple[float, float]
    label: str
    line: np.ndarray | None = None  # (2, 2)
    cross: bool = True


def nice_levels(lowest: float, highest: float, bands: int = 12) -> np.ndarray:
    """Level boundaries with a round step (1, 2, 2.5, 5 x 10^k) covering the range in
    about ``bands`` bands."""
    # A range this small is rounding noise, and rounded levels would come out equal.
    if not math.isfinite(lowest) or not math.isfinite(highest) or highest - lowest < 1e-6:
        highest = lowest + 1.0
    raw = (highest - lowest) / bands
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    start = math.floor(lowest / step) * step
    stop = math.ceil(highest / step) * step
    # Adding zero turns a rounded -0.0 into 0.0, which would read "-0" in a legend and a layer name.
    return np.round(np.arange(start, stop + step / 2, step), 10) + 0.0


def isofield(
    slab: Slab,
    values: np.ndarray,
    levels: np.ndarray,
    path: Path,
    title: str,
    unit: str,
    marks: list[Mark] = (),
    over_red: bool = False,
    site: np.ndarray | None = None,
    lows: list[tuple[tuple[float, float], str]] = (),
    limit: float | None = None,
    down_is_red: bool = False,
) -> Path:
    """Filled contours of node values over the slab, white around nodes without a value (NaN);
    ``levels`` fixes the colour scale; ``marks``: places to show. With ``over_red``, values
    above the last level are red (ratios over 1). ``site``: outlines for the key plan.
    ``lows``: small points with their values. ``limit``: a size, with a red contour where the
    values reach it in either direction, and a red outline when all of them are beyond it.
    ``down_is_red``: the hot end of the scale at the lowest values, the largest movement down."""
    fig, ax, side = _plan(slab, title, site)
    tri = Triangulation(slab.xy[:, 0], slab.xy[:, 1], slab.triangles())
    tri.set_mask(np.isnan(values[tri.triangles]).any(axis=1))
    cmap = _turbo(len(levels) - 1)
    if down_is_red:
        cmap = cmap.reversed()
    extend = "neither"
    if over_red:
        cmap = cmap.with_extremes(over=OVER_COLOR)
        extend = "max"
    clipped = np.clip(values, levels[0], None if over_red else levels[-1])
    if len(tri.get_masked_triangles()):
        filled = ax.tricontourf(tri, clipped, levels=levels, cmap=cmap, extend=extend)
        ax.tricontour(tri, clipped, levels=levels, colors="k", linewidths=0.2, alpha=0.4)
        if limit is not None:
            _limit_lines(ax, tri, clipped, slab, limit)
    else:  # no element has all its nodes on lines: only the scale and the marks are left
        filled = ScalarMappable(BoundaryNorm(levels, cmap.N), cmap)
    bar = fig.colorbar(filled, cax=side.inset_axes((0.0, 0.0, 0.16, 1.0)), ticks=levels)
    bar.set_label(unit)
    bar.ax.tick_params(labelsize=7)
    _outline_and_supports(ax, slab)
    for (x, y), text in lows:
        ax.plot([x], [y], marker="o", color="k", markersize=2.5, zorder=5)
        ax.annotate(text, (x, y), xytext=(3, 3), textcoords="offset points", fontsize=6, zorder=5)
    _draw_marks(fig, ax, marks)
    return _save(fig, path)


def _limit_lines(
    ax: object, tri: Triangulation, values: np.ndarray, slab: Slab, limit: float
) -> None:
    low, high = np.nanmin(values), np.nanmax(values)
    reached = [level for level in (-limit, limit) if low < level < high]
    if reached:
        ax.tricontour(tri, values, levels=reached, colors=MARK_COLOR, linewidths=1.6, zorder=4)
    elif low >= limit or high <= -limit:  # all of it is beyond the limit
        ax.add_collection(LineCollection(slab.boundary, colors=MARK_COLOR, linewidths=2.4))


def mosaic(
    slab: Slab,
    colors: list[str],
    band_of: np.ndarray,
    labels: list[str],
    path: Path,
    title: str,
    site: np.ndarray | None = None,
) -> Path:
    """Each element filled with the colour of its band (``band_of[i]``: index into
    ``colors`` for element i, -1 = no colour), with a legend of the bands."""
    fig, ax, side = _plan(slab, title, site)
    polys = [slab.xy[f] for f in slab.faces]
    facecolors = [colors[b] if b >= 0 else "white" for b in band_of]
    ax.add_collection(
        PolyCollection(polys, facecolors=facecolors, edgecolors="#00000022", linewidths=0.2)
    )
    _outline_and_supports(ax, slab)
    used = sorted({int(b) for b in band_of if b >= 0})
    handles = [
        Patch(facecolor=colors[b], edgecolor="k", linewidth=0.3, label=labels[b]) for b in used
    ]
    if handles:
        side.legend(handles=handles, loc="upper left", fontsize=7, frameon=False)
    return _save(fig, path)


def _plan(slab: Slab, title: str, site: np.ndarray | None) -> tuple[Figure, object, object]:
    """The figure: the slab large on the left; on the right the key plan above and, below
    it, an empty axes for a colour scale or a legend."""
    width = np.ptp(slab.xy[:, 0]) or 1.0
    height = np.ptp(slab.xy[:, 1]) or 1.0
    # The page it will fill, turned as the slab comes out larger; 0.8: the share of the width
    # left for the plan beside the key plan and the scale.
    long, short = PAGE_INCHES
    across = min(0.8 * long / width, short / height)
    upright = min(0.8 * short / width, long / height)
    fig = Figure(figsize=(long, short) if across >= upright else (short, long), dpi=DPI)
    FigureCanvasAgg(fig)
    grid = fig.add_gridspec(
        2, 2, width_ratios=(6, 1.5), height_ratios=(1, 2.2), wspace=0.12, hspace=0.12
    )
    ax = fig.add_subplot(grid[:, 0])
    ax.set_title(title, fontsize=10)
    ax.set_aspect("equal")
    ax.set_xlabel("X, м")
    ax.set_ylabel("Y, м")
    pad = 0.03 * max(width, height)
    ax.set_xlim(slab.xy[:, 0].min() - pad, slab.xy[:, 0].max() + pad)
    ax.set_ylim(slab.xy[:, 1].min() - pad, slab.xy[:, 1].max() + pad)
    ax.tick_params(labelsize=7)
    key = fig.add_subplot(grid[0, 1])
    if site is None:
        key.axis("off")
    else:
        _key_plan(key, slab, site)
    side = fig.add_subplot(grid[1, 1])
    side.axis("off")
    return fig, ax, side


def _key_plan(ax: object, slab: Slab, site: np.ndarray) -> None:
    """All slabs in thin grey, this one in red; a ring finds it when it is tiny."""
    ax.add_collection(LineCollection(site, colors="#999999", linewidths=0.3))
    ax.add_collection(LineCollection(slab.boundary, colors=MARK_COLOR, linewidths=1.0))
    x, y = (slab.xy.min(axis=0) + slab.xy.max(axis=0)) / 2
    ax.plot([x], [y], marker="o", markersize=9, mfc="none", mec=MARK_COLOR, mew=0.8)
    low, high = site.reshape(-1, 2).min(axis=0), site.reshape(-1, 2).max(axis=0)
    pad = 0.04 * float((high - low).max())
    ax.set_xlim(low[0] - pad, high[0] + pad)
    ax.set_ylim(low[1] - pad, high[1] + pad)
    ax.set_aspect("equal")
    ax.set_xticks(())
    ax.set_yticks(())
    ax.set_title("Где плита на плане", fontsize=7)


def _draw_marks(fig: Figure, ax: object, marks: list[Mark]) -> None:
    for m in marks:
        if m.line is not None:
            ax.plot(m.line[:, 0], m.line[:, 1], color="white", linewidth=3, zorder=5)
            ax.plot(m.line[:, 0], m.line[:, 1], color=MARK_COLOR, linewidth=1.4, marker="o",
                    markersize=3, zorder=6)  # fmt: skip
        if m.cross:
            ax.plot(*m.point, marker="x", color=MARK_COLOR, markersize=9, mew=2, zorder=7)
    _place_labels(fig, ax, marks)


def _place_labels(fig: Figure, ax: object, marks: list[Mark]) -> None:
    """Each label at the first place around its point that stays inside the plan and
    clear of the labels and points already there."""
    ax.apply_aspect()
    renderer = fig.canvas.get_renderer()
    frame = ax.get_window_extent(renderer)
    points = [ax.transData.transform(m.point) for m in marks]
    placed = []
    box = {"boxstyle": "round", "fc": "white", "ec": MARK_COLOR, "alpha": 0.9}
    for m in marks:
        note = ax.annotate(
            m.label, m.point, xytext=LABEL_PLACES[0], textcoords="offset points", fontsize=7.5,
            color=MARK_COLOR, bbox=box, zorder=8,
            arrowprops={"arrowstyle": "-", "color": MARK_COLOR, "linewidth": 0.6},
        )  # fmt: skip
        best, best_cost = LABEL_PLACES[0], math.inf
        for place in LABEL_PLACES if len(marks) <= PLACED_LABELS else LABEL_PLACES[:1]:
            note.set_position(place)
            note.set_horizontalalignment("left" if place[0] >= 0 else "right")
            note.set_verticalalignment("bottom" if place[1] >= 0 else "top")
            area = note.get_window_extent(renderer)
            cost = sum(area.overlaps(other) for other in placed) * 10
            cost += sum(area.contains(*p) for p in points) * 10
            outside = (
                area.x0 < frame.x0 or area.x1 > frame.x1 or area.y0 < frame.y0 or area.y1 > frame.y1
            )
            cost += 5 if outside else 0
            if cost < best_cost:
                best, best_cost = place, cost
            if cost == 0:
                break
        note.set_position(best)
        note.set_horizontalalignment("left" if best[0] >= 0 else "right")
        note.set_verticalalignment("bottom" if best[1] >= 0 else "top")
        placed.append(note.get_window_extent(renderer))


def _outline_and_supports(ax: object, slab: Slab) -> None:
    ax.add_collection(LineCollection(slab.boundary, colors="k", linewidths=0.9))
    if slab.on_foundation and len(slab.carried):  # what its settlement is compared between
        p = slab.xy[slab.carried]
        ax.plot(p[:, 0], p[:, 1], ".", color="k", markersize=2.5, linestyle="none")
    if slab.springs_all_over:  # a support at nearly every node would hide the slab in black
        return
    points = {"column": [], "wall": []}
    for i, kind in slab.support_kinds.items():
        points["wall" if kind in ("wall", "restraint") else "column"].append(slab.xy[i])
    if points["column"]:
        p = np.array(points["column"])
        ax.plot(p[:, 0], p[:, 1], "s", color="k", markersize=4, linestyle="none")
    if points["wall"]:
        p = np.array(points["wall"])
        ax.plot(p[:, 0], p[:, 1], ".", color="k", markersize=2.5, linestyle="none")


def band_colors(bands: int, down_is_red: bool = False) -> list[str]:
    """The colours of an isofield's bands, for a drawing of the same field."""
    from matplotlib.colors import to_hex

    colors = [to_hex(c) for c in _turbo(bands).colors]
    return colors[::-1] if down_is_red else colors


def _turbo(bands: int) -> ListedColormap:
    from matplotlib import colormaps

    return ListedColormap(colormaps["turbo"](np.linspace(0.08, 0.95, max(bands, 1))))


def _save(fig: Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    return path
