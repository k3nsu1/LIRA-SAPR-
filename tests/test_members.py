"""Bar members: beams cut into spans at their supports, deflections, column drift."""

import math
from itertools import pairwise

import pytest

from lira_sapr_mcp.postprocessing.members import (
    drift,
    joints,
    members,
    span_deflection,
    spans,
    supports,
)


def frame(bays=(6.0,), parts=4, height=3.0, overhang=0.0):
    """Columns (FE 10) at x = 0, bays..., a beam on top cut into ``parts`` per bay and an
    optional cantilever of ``overhang`` m beyond the last column. Bases restrained."""
    nodes, elements = {}, {}
    xs = [0.0]
    for b in bays:
        xs.append(xs[-1] + b)
    top = []
    nid = 1
    for i, x0 in enumerate(xs[:-1]):
        for p in range(parts):
            nodes[nid] = (x0 + bays[i] * p / parts, 0.0, height)
            top.append(nid)
            nid += 1
    nodes[nid] = (xs[-1], 0.0, height)
    top.append(nid)
    nid += 1
    if overhang:
        for p in (1, 2):
            nodes[nid] = (xs[-1] + overhang * p / 2, 0.0, height)
            top.append(nid)
            nid += 1
    el = 1
    for a, b in pairwise(top):
        elements[el] = (10, (a, b))
        el += 1
    beam = set(elements)
    bases = []
    for x in xs:
        (n,) = [k for k in top if nodes[k][0] == x]
        nodes[nid] = (x, 0.0, 0.0)
        bases.append(nid)
        elements[el] = (10, (nid, n))
        el += 1
        nid += 1
    return nodes, elements, beam, set(bases), top


def sag(nodes, top, x0, x1, f):
    """Z displacement of a parabola of depth f between x0 and x1, zero elsewhere."""
    out = {}
    for n in nodes:
        x = nodes[n][0]
        z = -4 * f * (x - x0) * (x1 - x) / (x1 - x0) ** 2 if x0 < x < x1 and n in top else 0.0
        out[n] = (0.0, 0.0, z)
    return out


def test_simple_beam_has_one_span_and_the_largest_deflection_in_the_middle():
    nodes, elements, beam, bases, top = frame()
    ms = members(nodes, elements, beam)
    assert len(ms) == 1 and not ms[0].column and len(ms[0].elements) == 4
    held = supports(nodes, elements, bases)
    (span,), unsupported = spans(ms, nodes, held)
    assert not unsupported and not span.cantilever and span.length == pytest.approx(6.0)
    d = span_deflection(span, nodes, sag(nodes, top, 0, 6, 0.024))
    assert nodes[d.node][0] == pytest.approx(3.0)
    assert d.f == pytest.approx(0.024) and d.fz == pytest.approx(-0.024)
    assert d.ratio == pytest.approx(250.0)


def test_a_continuous_beam_is_cut_at_every_column():
    nodes, elements, beam, bases, _ = frame(bays=(6.0, 4.0))
    ms = members(nodes, elements, beam)
    found, _ = spans(ms, nodes, supports(nodes, elements, bases))
    assert sorted(round(s.length, 6) for s in found) == [4.0, 6.0]


def test_settling_supports_do_not_count_as_deflection():
    # The whole beam drops 10 mm and tilts: the chord takes it, the span stays straight.
    nodes, elements, beam, bases, _ = frame()
    (span,), _ = spans(members(nodes, elements, beam), nodes, supports(nodes, elements, bases))
    moved = {n: (0.0, 0.0, -0.010 - 0.002 * nodes[n][0]) for n in nodes}
    d = span_deflection(span, nodes, moved)
    assert d.f == pytest.approx(0.0, abs=1e-12) and math.isinf(d.ratio)


def test_a_cantilever_is_measured_from_its_support():
    nodes, elements, beam, bases, _ = frame(overhang=1.5)
    found, _ = spans(members(nodes, elements, beam), nodes, supports(nodes, elements, bases))
    (cant,) = [s for s in found if s.cantilever]
    assert cant.length == pytest.approx(1.5) and nodes[cant.nodes[0]][0] == pytest.approx(6.0)
    disp = {n: (0.0, 0.0, -0.002 if n == cant.nodes[0] else 0.0) for n in nodes}
    disp[cant.nodes[-1]] = (0.0, 0.0, -0.012)
    d = span_deflection(cant, nodes, disp)
    assert d.node == cant.nodes[-1] and d.f == pytest.approx(0.010)
    assert d.ratio == pytest.approx(150.0)


def test_a_span_of_one_element_has_no_node_to_read():
    nodes, elements, beam, bases, top = frame(parts=1)
    (span,), _ = spans(members(nodes, elements, beam), nodes, supports(nodes, elements, bases))
    d = span_deflection(span, nodes, sag(nodes, top, 0, 6, 0.01))
    assert d.node is None and math.isnan(d.f)


def test_a_beam_with_no_support_rests_on_its_ends():
    nodes, elements, beam, _, _ = frame()
    beam_only = {k: v for k, v in elements.items() if k in beam}
    ms = members(nodes, beam_only)
    found, unsupported = spans(ms, nodes, supports(nodes, beam_only, set()))
    assert unsupported == [0] and found[0].length == pytest.approx(6.0)


def test_a_purlin_rests_on_the_rafters_given_as_supports():
    # A purlin along Y crossing two rafters along X at y = 0 and y = 6.
    nodes = {i: (0.0, 1.5 * i, 5.0) for i in range(5)}
    nodes |= {10: (-3.0, 0.0, 5.0), 11: (3.0, 0.0, 5.0), 12: (-3.0, 6.0, 5.0), 13: (3.0, 6.0, 5.0)}
    elements = {k + 1: (10, (k, k + 1)) for k in range(4)}
    elements |= {20: (10, (10, 0)), 21: (10, (0, 11)), 22: (10, (12, 4)), 23: (10, (4, 13))}
    purlin = {1, 2, 3, 4}
    ms = members(nodes, elements, purlin)
    assert len(ms) == 1 and len(ms[0].elements) == 4  # the rafters do not join it
    held = supports(nodes, elements, set(), on={20, 21, 22, 23})
    (span,), unsupported = spans(ms, nodes, held)
    assert not unsupported and span.length == pytest.approx(6.0)


def test_a_crossing_bar_does_not_continue_a_member():
    nodes = {1: (0, 0, 0), 2: (1, 0, 0), 3: (2, 0, 0), 4: (1, 1, 0)}
    elements = {1: (10, (1, 2)), 2: (10, (2, 3)), 3: (10, (2, 4))}
    ms = members(nodes, elements)
    assert sorted(len(m.elements) for m in ms) == [1, 2]


def test_springs_and_plates_are_not_bars():
    nodes = {1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0)}
    elements = {1: (56, (1,)), 2: (44, (1, 2, 3, 4)), 3: (55, (1, 2)), 4: (210, (1, 2))}
    (m,) = members(nodes, elements)
    assert m.elements == (4,)  # the non-linear universal bar 210 is a bar
    assert 1 in supports(nodes, elements, set())  # FE 56 holds its node


def test_a_column_drift_and_its_storeys():
    nodes = {1: (0, 0, 0), 2: (0, 0, 3), 3: (0, 0, 6), 4: (5, 0, 3), 5: (5, 0, 6)}
    elements = {1: (10, (1, 2)), 2: (10, (2, 3)), 3: (10, (2, 4)), 4: (10, (3, 5))}
    ms = members(nodes, elements)
    (k,) = [i for i, m in enumerate(ms) if m.column]
    disp = {1: (0, 0, 0), 2: (0.006, 0.008, 0), 3: (0.012, 0.016, 0), 4: (0, 0, 0), 5: (0, 0, 0)}
    dr = drift(k, ms[k], nodes, disp, joints(ms, elements))
    assert (dr.base, dr.top) == (1, 3) and dr.h == pytest.approx(6.0)
    assert dr.u == pytest.approx(0.020) and dr.ratio == pytest.approx(300.0)
    assert [(lo, hi) for lo, hi, *_ in dr.parts] == [(1, 2), (2, 3)]
    assert [round(u, 6) for *_, u in dr.parts] == [0.010, 0.010]
