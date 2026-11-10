"""Independent analytical and quadrature checks of the numerical contact model."""

import numpy as np
import pytest

from lira_sapr_mcp.postprocessing.contact import contact_pressure


def test_full_contact_matches_the_analytical_plane():
    p = contact_pressure(600, 30, -40, 3, 2)
    # a=N/A; slope_x=12 My/(A*l^2); slope_y=12 Mx/(A*b^2).
    assert p.coefficients == pytest.approx((100, -80 / 9, 15))
    assert p.contact_area == pytest.approx(6)
    assert p.p_max == pytest.approx(100 + 40 / 3 + 15)
    assert p.p_min == pytest.approx(100 - 40 / 3 - 15)
    assert p.edge_x_minus == pytest.approx(100 + 40 / 3)
    assert p.edge_y_plus == pytest.approx(115)


@pytest.mark.parametrize("eccentricity", [0, 1 / 6, 0.2, 0.4, 0.48])
def test_uniaxial_contact_matches_trapezoid_or_triangle(eccentricity):
    n, l, b = 500, 2, 3  # noqa: E741 - the norm's name for the length
    p = contact_pressure(n, 0, n * l * eccentricity, l, b)
    if eccentricity <= 1 / 6:
        expected_max = n / (l * b) * (1 + 6 * eccentricity)
        expected_min = n / (l * b) * (1 - 6 * eccentricity)
        expected_area = l * b
    else:
        contact_length = 3 * l * (0.5 - eccentricity)
        expected_max = 2 * n / (contact_length * b)
        expected_min = 0
        expected_area = contact_length * b
    assert p.p_max == pytest.approx(expected_max, rel=1e-7)
    assert p.p_min == pytest.approx(expected_min, abs=1e-8)
    assert p.contact_area == pytest.approx(expected_area, rel=1e-7)


@pytest.mark.parametrize("mx,my,l,b", [(150, 200, 2, 2), (350, 700, 4, 2), (-180, 100, 3, 2)])
def test_independent_midpoint_quadrature_verifies_all_three_resultants(mx, my, l, b):  # noqa: E741
    n = 500
    p = contact_pressure(n, mx, my, l, b)
    # Deliberately independent of polygon clipping and its analytical moments.
    count = 1600
    x = ((np.arange(count) + 0.5) / count - 0.5) * l
    y = ((np.arange(count) + 0.5) / count - 0.5) * b
    a, sx, sy = p.coefficients
    values = np.maximum(a + sx * x[None, :] + sy * y[:, None], 0)
    da = l * b / count**2
    assert values.sum() * da == pytest.approx(n, rel=3e-6)
    assert (values * x[None, :]).sum() * da == pytest.approx(my, rel=3e-6)
    assert (values * y[:, None]).sum() * da == pytest.approx(mx, rel=3e-6)
    assert abs(p.residual_n) <= n * 1e-10
    assert abs(p.residual_mx) <= n * b * 1e-10
    assert abs(p.residual_my) <= n * l * 1e-10
    assert p.p_min == 0
    assert 0 < p.contact_area < l * b


def test_reflections_axis_exchange_and_scaling():
    p = contact_pressure(500, 150, 200, 3, 2)
    reflected = contact_pressure(500, -150, -200, 3, 2)
    exchanged = contact_pressure(500, 200, 150, 2, 3)
    scaled = contact_pressure(1500, 900, 1200, 6, 4)
    assert reflected.coefficients == pytest.approx(
        (p.coefficients[0], -p.coefficients[1], -p.coefficients[2])
    )
    assert reflected.edge_x_plus == pytest.approx(p.edge_x_minus)
    assert reflected.edge_y_plus == pytest.approx(p.edge_y_minus)
    assert exchanged.coefficients == pytest.approx(
        (p.coefficients[0], p.coefficients[2], p.coefficients[1])
    )
    assert exchanged.p_max == pytest.approx(p.p_max)
    assert scaled.p_max == pytest.approx(p.p_max * 0.75)
    assert scaled.contact_area == pytest.approx(p.contact_area * 4)
    assert scaled.coefficients == pytest.approx(
        (p.coefficients[0] * 0.75, p.coefficients[1] * 0.375, p.coefficients[2] * 0.375)
    )


@pytest.mark.parametrize("mx,my", [(0, 500), (0, -501), (500, 0), (-600, 100)])
def test_resultant_on_or_outside_footprint_is_rejected(mx, my):
    with pytest.raises(ValueError, match="footprint"):
        contact_pressure(500, mx, my, 2, 2)


@pytest.mark.parametrize(
    "inputs", [(0, 0, 0, 2, 2), (-10, 0, 0, 2, 2), (10, 0, 0, 0, 2), (10, np.nan, 0, 2, 2)]
)
def test_invalid_input_is_rejected(inputs):
    with pytest.raises(ValueError):
        contact_pressure(*inputs)
