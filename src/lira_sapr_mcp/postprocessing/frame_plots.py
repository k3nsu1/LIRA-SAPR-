"""Pictures of a bar frame for the RPZ appendices: the scheme by stiffness, the deformed
scheme, and the spans coloured by L / f. Drawn in a parallel projection: an isometric view or
a plane (XZ, YZ, XY). PNG, 200 dpi, white background."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection

VIEWS = ("iso", "xz", "yz", "xy")
COS30, SIN30 = np.cos(np.radians(30)), np.sin(np.radians(30))
SPAN_BANDS = (
    (0, 150, "#d62728", "L/f < 150"),
    (150, 200, "#ff7f0e", "150–200"),
    (200, 250, "#bcbd22", "200–250"),
    (250, 400, "#2ca02c", "250–400"),
    (400, float("inf"), "#1f77b4", "L/f ≥ 400"),
)
GREY = "#b0b0b0"


def project(p: np.ndarray, view: str) -> np.ndarray:
    """Points (n, 3) to the picture plane (n, 2)."""
    p = np.atleast_2d(np.asarray(p, dtype=float))
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    if view == "iso":
        return np.column_stack([(x - y) * COS30, z + (x + y) * SIN30])
    if view == "xz":
        return np.column_stack([x, z])
    if view == "yz":
        return np.column_stack([y, z])
    if view == "xy":
        return np.column_stack([x, y])
    raise ValueError(f"view: one of {', '.join(VIEWS)}")


def figure(title: str):
    fig, ax = plt.subplots(figsize=(11.7, 8.3))  # A4 landscape, inches
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title, fontsize=12)
    return fig, ax


def save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def _segments(nodes: dict, bars: dict, view: str, shift: dict | None = None) -> tuple[list, list]:
    keys, segs = [], []
    for el, (a, b) in bars.items():
        pa = np.asarray(nodes[a], dtype=float) + (shift[a] if shift else 0)
        pb = np.asarray(nodes[b], dtype=float) + (shift[b] if shift else 0)
        segs.append(project(np.array([pa, pb]), view))
        keys.append(el)
    return keys, segs


def scheme(
    nodes: dict[int, tuple[float, float, float]],
    bars: dict[int, tuple[int, int]],
    stiffness_of: dict[int, int],
    supports: set[int],
    view: str,
    path: Path,
    title: str,
) -> Path:
    """Bars coloured by their stiffness number, supports as triangles."""
    fig, ax = figure(title)
    keys, segs = _segments(nodes, bars, view)
    numbers = sorted({stiffness_of.get(el, 0) for el in keys})
    cmap = plt.get_cmap("tab20", max(len(numbers), 1))
    colour = {s: cmap(i) for i, s in enumerate(numbers)}
    ax.add_collection(
        LineCollection(
            segs, colors=[colour[stiffness_of.get(el, 0)] for el in keys], linewidths=1.6
        )
    )
    if supports:
        pts = project(np.array([nodes[n] for n in sorted(supports) if n in nodes]), view)
        ax.plot(pts[:, 0], pts[:, 1], "k^", markersize=6, label="опоры")
    for s in numbers:
        ax.plot(
            [], [], color=colour[s], linewidth=3, label=f"жёсткость {s}" if s else "без жёсткости"
        )
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8, frameon=False)
    ax.autoscale()
    return save(fig, path)


def deformed(
    nodes: dict[int, tuple[float, float, float]],
    bars: dict[int, tuple[int, int]],
    displacement: dict[int, tuple[float, float, float]],
    view: str,
    path: Path,
    title: str,
    scale: float | None = None,
) -> tuple[Path, float]:
    """The scheme in grey and the displaced one in red, enlarged ``scale`` times (by default so
    that the largest displacement shows as 5 % of the model's size). Returns the scale."""
    used = {n for ab in bars.values() for n in ab}
    p = np.array([nodes[n] for n in used])
    size = float(np.ptp(p, axis=0).max()) or 1.0
    d = {n: np.asarray(displacement.get(n, (0, 0, 0)), dtype=float) for n in used}
    largest = max(used, key=lambda n: float(np.linalg.norm(d[n])))
    top = float(np.linalg.norm(d[largest]))
    if scale is None:
        scale = 0.05 * size / top if top > 0 else 1.0
    fig, ax = figure(f"{title} (масштаб перемещений ×{scale:.0f})")
    _, plain = _segments(nodes, bars, view)
    ax.add_collection(LineCollection(plain, colors=GREY, linewidths=0.8))
    _, moved = _segments(nodes, bars, view, {n: scale * d[n] for n in used})
    ax.add_collection(LineCollection(moved, colors="#d62728", linewidths=1.4))
    at = project(np.asarray(nodes[largest]) + scale * d[largest], view)[0]
    ax.annotate(
        f"узел {largest}: {top * 1000:.1f} мм".replace(".", ","),
        at,
        fontsize=9,
        xytext=(8, 8),
        textcoords="offset points",
    )
    ax.autoscale()
    return save(fig, path), scale


LABEL_STEP = 12  # points between labels of spans drawn at nearly the same place
NEAR = 0.04  # of the drawing's size: labels closer than this are stacked


def _label_places(
    nodes: dict[int, tuple[float, float, float]],
    spans: list[tuple[tuple[int, ...], float]],
    view: str,
    marked: int,
) -> list[tuple[np.ndarray, int]]:
    """Where to sign the ``marked`` worst spans: the middle of each in the drawing, and how
    many labels up to shift it so that it does not cover a label signed near it."""
    if not spans:
        return []
    every = project(np.array([nodes[n] for row, _ in spans for n in row]), view)
    size = float(np.max(np.ptp(every, axis=0))) or 1.0
    places: list[tuple[np.ndarray, int]] = []
    for row, _ in sorted(spans, key=lambda s: s[1])[:marked]:
        mid = project(np.array([nodes[n] for n in row]), view).mean(axis=0)
        shift = sum(1 for p, _ in places if np.linalg.norm(p - mid) < NEAR * size)
        places.append((mid, shift))
    return places


def spans_map(
    nodes: dict[int, tuple[float, float, float]],
    bars: dict[int, tuple[int, int]],
    spans: list[tuple[tuple[int, ...], float]],
    view: str,
    path: Path,
    title: str,
    marked: int = 3,
) -> Path:
    """Bars in grey; each span (its nodes along the bar, L / f) drawn in the colour of its L / f
    band; the ``marked`` worst signed with their L / f."""
    fig, ax = figure(title)
    _, plain = _segments(nodes, bars, view)
    ax.add_collection(LineCollection(plain, colors=GREY, linewidths=0.8))
    for row, ratio in spans:
        band = next(b for b in SPAN_BANDS if b[0] <= ratio < b[1])
        pts = project(np.array([nodes[n] for n in row]), view)
        ax.plot(pts[:, 0], pts[:, 1], color=band[2], linewidth=2.4)
    for k, (mid, shift) in enumerate(_label_places(nodes, spans, view, marked), start=1):
        ratio = sorted(s[1] for s in spans)[k - 1]
        ax.annotate(
            f"{k}: L/f = {ratio:.0f}",
            mid,
            fontsize=9,
            xytext=(6, 6 + LABEL_STEP * shift),
            textcoords="offset points",
        )
    for _, _, c, label in SPAN_BANDS:
        ax.plot([], [], color=c, linewidth=3, label=label)
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1.0, 1.0),
        fontsize=8,
        frameon=False,
        title="Цвет — для наглядности,\nпредел задаёт проект",
    )
    ax.autoscale()
    return save(fig, path)
