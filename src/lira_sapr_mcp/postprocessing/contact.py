"""Numerical compression-only contact under a rigid rectangular footing.

This is the adopted planar pressure model p=max(a+b*x+c*y, 0), not a normative
formula. The plane is determined by force and moment equilibrium. Coordinates are
centred on the footing: x follows length l, y follows width b; My=int(x*p),
Mx=int(y*p). Inputs: kN, kN*m, m. Outputs: kPa, m2, and physical residuals.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ContactPressure:
    """Verified equilibrium of a nonnegative plane on the footing rectangle."""

    p_max: float
    p_min: float
    edge_x_minus: float
    edge_x_plus: float
    edge_y_minus: float
    edge_y_plus: float
    contact_area: float
    coefficients: tuple[float, float, float]  # kPa, kPa/m, kPa/m
    residual_n: float  # kN
    residual_mx: float  # kN*m
    residual_my: float  # kN*m


def _contact_matrix(q: np.ndarray) -> np.ndarray:
    """Exact integrals of [1,x,y] outer [1,x,y] on the positive half-plane."""
    rectangle = [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
    polygon = []
    previous = np.array(rectangle[-1])
    previous_value = q[0] + q[1:] @ previous
    for point in rectangle:
        current = np.array(point)
        value = q[0] + q[1:] @ current
        if (value > 0) != (previous_value > 0):
            fraction = previous_value / (previous_value - value)
            polygon.append(previous + fraction * (current - previous))
        if value > 0:
            polygon.append(current)
        previous, previous_value = current, value
    matrix = np.zeros((3, 3))
    if len(polygon) < 3:
        return matrix
    # Triangulation avoids subtracting almost equal polygon boundary integrals for
    # a narrow contact strip. Barycentric moments integrate quadratics exactly.
    for i in range(1, len(polygon) - 1):
        vertices = np.array([polygon[0], polygon[i], polygon[i + 1]])
        u, v = vertices[1] - vertices[0], vertices[2] - vertices[0]
        area = abs(u[0] * v[1] - u[1] * v[0]) / 2
        total = vertices.sum(axis=0)
        matrix[0, 0] += area
        matrix[0, 1:] += area * total / 3
        matrix[1:, 0] += area * total / 3
        matrix[1:, 1:] += area * (np.outer(total, total) + vertices.T @ vertices) / 12
    return matrix


def contact_pressure(
    n: float,
    mx: float,
    my: float,
    l: float,  # noqa: E741 - the norm's name for the length
    b: float,
) -> ContactPressure:
    """Solve compression-only contact and verify N, Mx, My before returning.

    The pressure is linear only in contact, zero elsewhere. N must already include
    any footing/soil weight. A resultant on or outside the footprint cannot have a
    finite contact solution. ValueError rejects inputs; ArithmeticError rejects
    nonconvergence or failed equilibrium. No unchecked pressure is returned.
    """
    if not all(np.isfinite(v) for v in (n, mx, my, l, b)) or min(n, l, b) <= 0:
        raise ValueError("contact requires finite loads, positive compression and dimensions")
    target = np.array([1.0, my / n / l, mx / n / b])
    if not np.all(np.isfinite(target)) or np.any(np.abs(target[1:]) >= 0.5):
        raise ValueError("the resultant is on or outside the footing footprint")
    q = target * np.array([1.0, 12.0, 12.0])
    tolerance = 1e-10
    for _ in range(120):
        matrix = _contact_matrix(q)
        residual = matrix @ q - target
        if np.max(np.abs(residual)) <= tolerance:
            break
        try:
            step = np.linalg.solve(matrix, residual)
        except np.linalg.LinAlgError as exc:
            raise ArithmeticError("contact equilibrium is numerically singular") from exc
        old_error = np.linalg.norm(residual)
        damping = 1.0
        for _ in range(45):
            trial = q - damping * step
            trial_residual = _contact_matrix(trial) @ trial - target
            if np.all(np.isfinite(trial_residual)) and np.linalg.norm(trial_residual) < old_error:
                q = trial
                break
            damping *= 0.5
        else:
            raise ArithmeticError("contact equilibrium did not converge")
    else:
        raise ArithmeticError("contact equilibrium did not converge")
    matrix = _contact_matrix(q)
    residual = matrix @ q - target
    if not np.all(np.isfinite(residual)) or np.max(np.abs(residual)) > tolerance:
        raise ArithmeticError("contact force and moment equilibrium was not verified")
    mean = n / l / b
    coefficients = (float(mean * q[0]), float(mean * q[1] / l), float(mean * q[2] / b))

    def pressure(x: float, y: float) -> float:
        return float(mean * max(q[0] + q[1] * x + q[2] * y, 0.0))

    corners = [pressure(x, y) for x in (-0.5, 0.5) for y in (-0.5, 0.5)]
    result = ContactPressure(
        max(corners),
        min(corners),
        pressure(-0.5, 0),
        pressure(0.5, 0),
        pressure(0, -0.5),
        pressure(0, 0.5),
        float(matrix[0, 0] * l * b),
        coefficients,
        float(residual[0] * n),
        float(residual[2] * n * b),
        float(residual[1] * n * l),
    )
    if not all(np.isfinite(v) for v in (*coefficients, result.p_max, result.contact_area)):
        raise ArithmeticError("contact pressure exceeds numerical range")
    return result
