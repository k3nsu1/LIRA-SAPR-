"""Pressure under a rectangular footing and its checks by SP RK 5.01-102-2013, and the
multipliers of a load combination (RSN) fitted from displacements.

Footing (SP RK 5.01-102-2013): the mean pressure p = N / A + γmt·d is at most R (4.6.1); the
edge pressure from the moment along each axis at most 1.2R and at a corner 1.5R (4.6.19); the
resultant stays within the core, e <= l/6 (pmin >= 0), and for crane buildings of 75 t and more,
open crane trestles over 15 t, tower structures and wherever R <= 150 kPa pmin / pmax >= 0.25
(4.6.20). Edge pressures follow the linear law (4.6.21, formulas 12 and 13): within the core
p = Ntot / A · (1 ± 6 ex / l ± 6 ey / b); beyond it, along one axis, a triangle over the
compressed length 3 (l / 2 - e): pmax = 2 Ntot / (3 b (l / 2 - e)). N excludes the weight of
the footing and the soil on it, which is γmt·d per square metre. Units: kN, m, kPa.

Load combinations: LIRA's API does not give the coefficients of an RSN; its displacements are
the sum of the load cases' with them (the author found ~1e-16 residual on the owner's model), so
they are fitted by least squares from the displacements of a few nodes.
"""

from dataclasses import dataclass, field

import numpy as np

EDGE = 1.2  # of R, at an edge (4.6.19)
CORNER = 1.5  # of R, at a corner (4.6.19)
TRAPEZOID = 0.25  # pmin / pmax at least, for the structures of 4.6.20


@dataclass(frozen=True)
class Pressure:
    """Pressures (kPa) under a footing and what they are checked against."""

    n_total: float  # kN, N plus the footing and soil
    p: float  # mean
    p_max: float  # largest at an edge or corner
    p_min: float
    edge_x: float  # largest edge pressure from the moment along l
    edge_y: float  # along b
    ex: float  # m, eccentricity along l
    ey: float  # along b
    r: float
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # what, ok, numbers

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def pressure(
    n: float,
    mx: float,
    my: float,
    l: float,  # noqa: E741 - the norm's name for the length
    b: float,
    d: float,
    r: float,
    gamma_mt: float = 20.0,
    trapezoid: bool = False,
) -> Pressure:
    """N (kN, down positive) and moments (kN·m): ``my`` turns the footing along its length l,
    ``mx`` along its width b. ``trapezoid``: the structure is of those 4.6.20 asks
    pmin / pmax >= 0.25 of."""
    if l <= 0 or b <= 0 or r <= 0:
        raise ValueError("l, b and R must be above zero")
    area = l * b
    total = n + gamma_mt * d * area
    if total <= 0:
        raise ValueError("the footing is lifted: N with its weight is not a compression")
    ex, ey = abs(my) / total, abs(mx) / total
    mean = total / area
    within = ex <= l / 6 + 1e-12 and ey <= b / 6 + 1e-12
    edge_x = mean * (1 + 6 * ex / l) if ex <= l / 6 + 1e-12 else 2 * total / (3 * b * (l / 2 - ex))
    edge_y = mean * (1 + 6 * ey / b) if ey <= b / 6 + 1e-12 else 2 * total / (3 * l * (b / 2 - ey))
    if within:
        p_max = mean * (1 + 6 * ex / l + 6 * ey / b)
        p_min = mean * (1 - 6 * ex / l - 6 * ey / b)
    else:  # out of the core: the edge of the triangle, nothing at the other side
        p_max, p_min = max(edge_x, edge_y), 0.0
    checks = [
        ("p ≤ R (п. 4.6.1)", mean <= r, f"{mean:.1f} ≤ {r:g}"),
        (
            "краевое ≤ 1,2R (п. 4.6.19)",
            max(edge_x, edge_y) <= EDGE * r,
            f"{max(edge_x, edge_y):.1f} ≤ {EDGE * r:.1f}",
        ),
        ("угловое ≤ 1,5R (п. 4.6.19)", p_max <= CORNER * r, f"{p_max:.1f} ≤ {CORNER * r:.1f}"),
        (
            "e ≤ l/6, pmin ≥ 0 (п. 4.6.20)",
            within and p_min >= -1e-9,
            f"e = {ex:.3f} / {ey:.3f} м; l/6 = {l / 6:.3f}, b/6 = {b / 6:.3f}",
        ),
    ]
    if trapezoid or r <= 150:
        ratio = p_min / p_max if p_max > 0 else 0.0
        checks.append(
            ("pmin / pmax ≥ 0,25 (п. 4.6.20)", ratio >= TRAPEZOID - 1e-12, f"{ratio:.2f} ≥ 0,25")
        )
    return Pressure(total, mean, p_max, p_min, edge_x, edge_y, ex, ey, r, checks)


@dataclass(frozen=True)
class Fit:
    """Multipliers of load cases in a combination, fitted from displacements, and how well:
    ``residual`` is the largest misfit relative to the largest displacement."""

    cases: list[int]
    multipliers: list[float]
    residual: float


def fit_combination(
    by_case: dict[int, np.ndarray], combination: np.ndarray, tolerance: float = 1e-6
) -> Fit:
    """``by_case``: per load case, the displacements of the sample nodes (any shape, the same
    for all); ``combination``: those of the RSN. Multipliers below ``tolerance`` are zero."""
    cases = sorted(by_case)
    a = np.column_stack([np.ravel(by_case[c]) for c in cases])
    y = np.ravel(combination)
    k, *_ = np.linalg.lstsq(a, y, rcond=None)
    k = np.where(np.abs(k) < tolerance, 0.0, k)
    scale = float(np.max(np.abs(y))) if y.size and np.max(np.abs(y)) > 0 else 1.0
    residual = float(np.max(np.abs(a @ k - y))) / scale if y.size else 0.0
    return Fit(cases, [float(v) for v in k], residual)
