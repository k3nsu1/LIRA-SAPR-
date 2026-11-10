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


def test_a_load_case_with_a_noise_multiplier_is_not_in_the_rsn():
    """K1.7: the RSN held cases 1 and 2 only; the canopy case 3 came out -0.0001 from the
    rounding of the results and was taken as part of the RSN."""
    rng = np.random.default_rng(5)
    by_case = {c: rng.normal(size=(40, 6)) for c in (1, 2)}
    by_case[3] = np.zeros((40, 6))
    by_case[3][:4] = 0.02 * rng.normal(size=(4, 6))  # a small part of the model
    rsn = 1.0 * by_case[1] + 0.85 * by_case[2] + np.round(rng.normal(size=(40, 6)) * 1e-7, 9)
    fit = fit_combination(by_case, rsn)
    assert fit.multipliers[2] == 0.0
    assert fit.multipliers[:2] == pytest.approx([1.0, 0.85], abs=1e-5)
    # a case that is in the RSN, however small its displacements, stays
    fit = fit_combination(by_case, rsn + 1.0 * by_case[3])
    assert fit.multipliers[2] == pytest.approx(1.0, abs=1e-3)


def test_proportional_cases_do_not_identify_individual_multipliers():
    rng = np.random.default_rng(1)
    base = rng.normal(size=300)
    cases = {1: base, 2: base * 0.5, 3: rng.normal(size=300)}
    fit = fit_combination(cases, 1.1 * cases[1] + 1.3 * cases[2] + 0.9 * cases[3])
    assert fit.residual < 1e-12
    assert not fit.identifiable
    assert fit.ambiguous_cases == [1, 2]


def test_empty_displacements_do_not_prove_a_combination():
    fit = fit_combination({1: np.array([])}, np.array([]))
    assert not fit.identifiable


def test_nearly_proportional_cases_are_ambiguous_at_result_precision():
    rng = np.random.default_rng(1)
    base = rng.normal(size=300)
    cases = {1: base, 2: base * 0.5 + rng.normal(size=300) * 1e-7}
    fit = fit_combination(cases, 1.1 * cases[1] + 1.3 * cases[2])
    assert not fit.identifiable
    assert fit.ambiguous_cases == [1, 2]


@pytest.mark.parametrize("mx,my", [(500 / 3, 500 / 3), (150, 200)])
def test_biaxial_load_outside_diamond_core_has_no_pressure_verdict(mx, my):
    p = pressure(500, mx, my, 2, 2, 0, 300)
    assert p.p == 125
    assert p.p_max is None and p.p_min is None
    assert not p.ok
    assert any("не определено" in text for _, _, text in p.checks)


@pytest.mark.parametrize("moment", [500, 600])
def test_resultant_on_or_outside_footprint_has_no_contact_solution(moment):
    p = pressure(500, 0, moment, 2, 2, 0, 300)
    assert not p.ok and p.p_max is None
