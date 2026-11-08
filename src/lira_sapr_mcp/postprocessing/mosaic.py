"""Reinforcement mosaics of slabs: scales that sort elements into bands, and the
outlines of the zones of a band for drawings.

Two scales, as engineers use them: steps of required area (cm2/m), as LIRA colours its
own mosaics; or "background + add-ons": the background mesh everywhere and the
add-on bars (diameter and spacing) an element needs on top of it.
"""

import math
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from lira_sapr_mcp.postprocessing.mesh import Slab, polygon_area

COMPONENTS = {
    "AS1": "AS1 — нижняя арматура по X",
    "AS2": "AS2 — верхняя арматура по X",
    "AS3": "AS3 — нижняя арматура по Y",
    "AS4": "AS4 — верхняя арматура по Y",
}
SHORT = "#d00000"  # more is needed than the largest add-on gives
FAILED = "#000000"  # LIRA could not design the section
BACKGROUND = "#f2f2f2"
NO_DESIGN = -1.0  # an element LIRA designed nothing for: no colour, in no band
ADDON_COLORS = ["#4e79a7", "#59a14f", "#edc948", "#f28e2b", "#b07aa1", "#76b7b2", "#9c755f",
                "#ff9da7", "#bab0ac", "#86bcb6"]  # fmt: skip


@dataclass(frozen=True)
class Bars:
    """Bars of one diameter (mm) at one spacing (mm)."""

    diameter: float
    spacing: float

    @property
    def area(self) -> float:
        """cm2 per meter."""
        return math.pi * self.diameter**2 / 4 * 1000 / self.spacing / 100

    @property
    def label(self) -> str:
        return f"Ø{self.diameter:g} с{self.spacing:g}"

    @property
    def code(self) -> str:  # for DXF layer names
        return f"D{self.diameter:g}s{self.spacing:g}"


@dataclass(frozen=True)
class Scale:
    """Bands of a mosaic: ``labels[i]`` and ``colors[i]`` of band i; ``codes[i]`` names
    its DXF layer; ``drawn[i]``: whether band i is a zone of the drawing."""

    kind: str
    labels: list[str]
    colors: list[str]
    codes: list[str]
    drawn: list[bool]
    levels: list[float] | None = None
    background: Bars | None = None
    addons: list[Bars] | None = None

    def bands(self, values: np.ndarray) -> np.ndarray:
        """Band of each value (cm2/m; NaN = LIRA could not design it; NO_DESIGN =
        LIRA designed nothing for it); -1 = no colour."""
        out = np.full(len(values), -1, dtype=int)
        failed = np.isnan(values)
        v = np.nan_to_num(values, nan=0.0)
        if self.kind == "steps":
            levels = np.asarray(self.levels)
            out = np.where(v > 0, np.searchsorted(levels, v, side="right") - 1, -1)
            out = np.clip(out, -1, len(levels) - 1)
        else:
            totals = np.array([self.background.area + a.area for a in self.addons])
            k = np.searchsorted(totals, v - 1e-9, side="left")  # first add-on that suffices
            out = np.where(v <= self.background.area + 1e-9, 0, 1 + k)
        out[failed] = len(self.labels) - 1
        out[values == NO_DESIGN] = -1
        return out


def step_scale(levels: list[float]) -> Scale:
    """Bands [l0, l1), [l1, l2) ... and >= the last level; values of 0 stay white."""
    levels = sorted(float(x) for x in levels)
    if len(levels) < 2 or levels[0] < 0:
        raise ValueError("levels: at least two non-negative values, in cm2/m")
    labels = [f"{_ru(a)}–{_ru(b)}" for a, b in pairwise(levels)]
    labels.append(f"≥ {_ru(levels[-1])}")
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    turbo = colormaps["turbo"](np.linspace(0.12, 0.92, len(labels)))
    colors = [to_hex(rgba) for rgba in turbo]
    codes = [f"{_code(a)}-{_code(b)}" for a, b in pairwise(levels)]
    codes.append(f"over{_code(levels[-1])}")
    return Scale(
        "steps",
        [*labels, "сечение не подобрано"],
        [*colors, FAILED],
        [*codes, "failed"],
        [True] * (len(labels) + 1),
        levels=levels,
    )


def addon_scale(background: Bars, addons: list[Bars]) -> Scale:
    """Band 0: the background is enough; band k: background + the k-th add-on (by
    area); then: more than the largest add-on gives; then: not designed."""
    if not addons:
        raise ValueError("addons: at least one add-on, e.g. diameter 12, spacing 200")
    addons = sorted(addons, key=lambda a: a.area)
    bg = background.area
    labels = [f"фон {background.label} ({_ru(bg)} см²/м)"]
    labels += [f"фон + {a.label} ({_ru(bg + a.area)} см²/м)" for a in addons]
    labels += [f"больше {_ru(bg + addons[-1].area)} см²/м", "сечение не подобрано"]
    colors = [BACKGROUND, *(ADDON_COLORS[i % len(ADDON_COLORS)] for i in range(len(addons)))]
    colors += [SHORT, FAILED]
    codes = ["background", *(f"add_{a.code}" for a in addons), "short", "failed"]
    drawn = [False] + [True] * (len(addons) + 2)
    return Scale("addons", labels, colors, codes, drawn, background=background, addons=addons)


def zone_areas(slab: Slab, band_of: np.ndarray, bands: int) -> list[float]:
    """Plan area (m2) of the elements of each band."""
    areas = [0.0] * bands
    for f, b in zip(slab.faces, band_of, strict=True):
        if b >= 0:
            areas[b] += polygon_area(slab.xy[f])
    return areas


def _ru(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _code(x: float) -> str:
    return f"{x:g}".replace(".", "p")
