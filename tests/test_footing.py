"""Pressure under a footing by SP RK 5.01-102-2013 and fitting RSN multipliers."""

import numpy as np
import pytest

from lira_sapr_mcp.postprocessing.footing import fit_combination, pressure


def test_a_centred_load_gives_the_mean_pressure_everywhere():
    # 2 x 2 m, 1.5 m deep: 4 m2, weight 20 * 1.5 * 4 = 120 kN; with 680 kN -> 200 kPa.
    p = pressure(680, 0, 0, 2.0, 2.0, 1.5, 250)
    assert p.n_total == pytest.approx(800) and p.p == pytest.approx(200)
    assert p.p_max == pytest.approx(200) and p.p_min == pytest.approx(200)
    assert p.ok and len(p.checks) == 4


def test_a_moment_within_the_core_gives_a_trapezoid():
    # 800 kN total on 2 x 2 m, M = 100 kN·m along l: e = 0.125 < 2/6; p = 200 (1 ± 0.375).
    p = pressure(680, 0, 100, 2.0, 2.0, 1.5, 250)
    assert p.ex == pytest.approx(0.125) and p.ey == 0
    assert p.p_max == pytest.approx(275) and p.p_min == pytest.approx(125)
    assert p.edge_x == pytest.approx(275)
    assert [ok for _, ok, _ in p.checks] == [True, True, True, True]


def test_an_edge_over_1_2r_fails_and_a_corner_is_checked_at_1_5r():
    p = pressure(680, 100, 100, 2.0, 2.0, 1.5, 220)
    # e = 0.125 both ways: corner 200 (1 + 0.375 + 0.375) = 350; edge 275 > 1.2 * 220 = 264
    assert p.p_max == pytest.approx(350)
    checks = dict((what, ok) for what, ok, _ in p.checks)
    assert checks["краевое ≤ 1,2R (п. 4.6.19)"] is False
    assert checks["угловое ≤ 1,5R (п. 4.6.19)"] is False  # 350 > 330
    assert not p.ok


def test_out_of_the_core_the_pressure_is_a_triangle():
    # e = 0.5 > 2/6 along l: pmax = 2 * 800 / (3 * 2 * (1 - 0.5)) = 533.3
    p = pressure(680, 0, 400, 2.0, 2.0, 1.5, 300)
    assert p.p_max == pytest.approx(1600 / 3) and p.p_min == 0
    assert dict((w, ok) for w, ok, _ in p.checks)["e ≤ l/6, pmin ≥ 0 (п. 4.6.20)"] is False


def test_a_crane_trestle_or_weak_soil_asks_a_quarter_at_least():
    p = pressure(680, 0, 100, 2.0, 2.0, 1.5, 250, trapezoid=True)
    assert p.checks[-1][0].startswith("pmin / pmax") and p.checks[-1][1]  # 125 / 275 = 0.45
    weak = pressure(680, 0, 150, 2.0, 2.0, 1.5, 150)  # R <= 150: always asked
    assert weak.checks[-1][0].startswith("pmin / pmax")


def test_a_lifted_footing_is_refused():
    with pytest.raises(ValueError, match="lifted"):
        pressure(-500, 0, 0, 2.0, 2.0, 1.0, 200)


def test_rsn_multipliers_are_fitted_exactly_from_displacements():
    rng = np.random.default_rng(1)
    cases = {c: rng.normal(size=(30, 6)) for c in (1, 2, 3, 4)}
    k = {1: 1 / 1.1, 2: 0.0, 3: 0.35 / 1.2, 4: 0.5 / 1.4}
    rsn = sum(k[c] * cases[c] for c in cases)
    fit = fit_combination(cases, rsn)
    assert fit.cases == [1, 2, 3, 4]
    assert fit.multipliers == pytest.approx([k[c] for c in (1, 2, 3, 4)])
    assert fit.multipliers[1] == 0.0 and fit.residual < 1e-12
