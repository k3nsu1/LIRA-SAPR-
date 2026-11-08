"""Settlement of a slab on an elastic foundation: its zones and relative differences."""

from itertools import pairwise

import numpy as np
import pytest

from lira_sapr_mcp.postprocessing.mesh import find_slabs
from lira_sapr_mcp.postprocessing.settlement import relative_differences, zones_over
from test_slab_mesh import grid

RELATIVE = 0.003  # a project limit of the relative settlement difference
SETTLED = 0.15  # m, a project limit of the settlement


def foundation(columns, nx=24, ny=4, size=1.0):
    """A slab on springs with columns standing at the given (i, j) of its grid."""
    nodes, elements, number = grid(nx, ny, size=size)
    more = {5000 + k: (51, (n,)) for k, n in enumerate(number.values())}
    for k, (i, j) in enumerate(columns):
        x, y, _ = nodes[number[i, j]]
        nodes[9000 + k] = (x, y, 3.0)
        more[6000 + k] = (10, (number[i, j], 9000 + k))
    (slab, *_) = find_slabs(nodes, elements | more)
    assert slab.on_foundation
    return slab


def test_only_the_largest_difference_is_shown_within_the_limit():
    slab = foundation([(0, 0), (6, 0), (12, 0)])
    s = 0.001 * slab.xy[:, 0]  # 0.001 along X
    s[np.flatnonzero(slab.xy[:, 0] > 6)] = 0.006 + 0.002 * (slab.xy[slab.xy[:, 0] > 6, 0] - 6)
    (place,) = relative_differences(slab, s, RELATIVE)
    assert place.ratio == pytest.approx(0.002)
    assert place.length == pytest.approx(6)
    assert {(place.a.x, place.a.y), (place.b.x, place.b.y)} == {(6.0, 0.0), (12.0, 0.0)}
    assert not place.a.centre and slab.xy[place.a.node, 0] == place.a.x


def test_every_place_over_the_limit_is_shown():
    # Columns every 6 m along X, a slope of 0.005 all along: each bay is over 0.003.
    slab = foundation([(i, 0) for i in range(0, 25, 6)])
    s = 0.005 * slab.xy[:, 0]
    places = relative_differences(slab, s, RELATIVE)
    assert all(p.ratio == pytest.approx(0.005) for p in places)
    covered = {int(min(p.a.x, p.b.x) // 6) for p in places if p.length == 6}
    assert len(covered) >= 2  # a bay away is another place
    assert len(places) >= 3


def test_a_long_pair_over_the_limit_does_not_hide_a_short_one_elsewhere():
    slab = foundation([(0, 0), (12, 0), (18, 0), (19, 0)])
    s = np.zeros(len(slab.nodes))
    s[slab.xy[:, 0] >= 12] = 0.06  # 0.005 over the 12 m pair...
    s[slab.xy[:, 0] >= 19] = 0.07  # ...and 0.01 over the last metre
    places = relative_differences(slab, s, RELATIVE)
    assert places[0].ratio == pytest.approx(0.01) and places[0].length == pytest.approx(1)
    assert any(p.length == pytest.approx(12) for p in places)


def test_nothing_or_one_point_standing_has_nothing_to_compare():
    slab = foundation([])
    assert relative_differences(slab, np.zeros(len(slab.nodes)), RELATIVE) == []
    slab = foundation([(3, 3)])
    assert relative_differences(slab, np.zeros(len(slab.nodes)), RELATIVE) == []


def built(columns=(), walls=(), rigid=(), nx=24, ny=4):
    """A foundation with columns at (i, j), walls along the runs of (i, j) given and rigid bodies
    over the (i, j) given, each with a column standing on its first node."""
    nodes, elements, number = grid(nx, ny)
    more = {5000 + k: (51, (n,)) for k, n in enumerate(number.values())}
    next_node, next_el = 9000, 6000

    def above(at):
        nonlocal next_node
        next_node += 1
        x, y, _ = nodes[number[at]]
        nodes[next_node] = (x, y, 3.0)
        return next_node

    for at in (*columns, *(body[0] for body in rigid)):
        next_el += 1
        more[next_el] = (10, (number[at], above(at)))
    for wall in walls:
        for p, q in pairwise(wall):
            next_el += 1
            more[next_el] = (44, (number[p], number[q], above(q), above(p)))
    groups = [{number[at] for at in body} for body in rigid]
    (slab, *_) = find_slabs(nodes, elements | more, groups=groups)
    assert slab.on_foundation
    return slab


SQUARE = [(9, 1), (10, 1), (10, 2), (9, 2)]


def test_a_column_on_a_rigid_body_is_one_point_at_its_centre():
    # The body tilts: 44 and 46 mm at its sides. Nothing is measured inside it, only from its
    # centre, the mean of its nodes.
    slab = built(columns=[(3, 1)], rigid=[SQUARE])
    s = np.zeros(len(slab.nodes))
    s[slab.xy[:, 0] == 9] = 0.044
    s[slab.xy[:, 0] == 10] = 0.046
    (place,) = relative_differences(slab, s, limit=1.0)
    centre, column = (place.a, place.b) if place.a.centre else (place.b, place.a)
    assert (centre.x, centre.y) == (9.5, 1.5)
    assert centre.settlement == pytest.approx(0.045)
    assert (column.x, column.y) == (3.0, 1.0)
    assert place.length == pytest.approx(np.hypot(6.5, 0.5))
    assert place.ratio == pytest.approx(0.045 / place.length)


def test_a_rigid_body_alone_has_nothing_to_compare():
    slab = built(rigid=[SQUARE])
    assert relative_differences(slab, slab.xy[:, 0] * 0.01, RELATIVE) == []


def test_a_short_pylon_is_not_compared_inside_itself():
    slab = built(walls=[[(5, 1), (6, 1), (7, 1)]])
    assert relative_differences(slab, slab.xy[:, 0] * 0.01, RELATIVE) == []


def test_a_long_wall_is_compared_along_itself_from_three_metres():
    wall = [(i, 1) for i in range(2, 10)]
    slab = built(walls=[wall])
    s = np.zeros(len(slab.nodes))
    s[slab.xy[:, 0] >= 5] = 0.03  # a step of 30 mm: 0.03 over the neighbours, if they counted
    (place,) = relative_differences(slab, s, limit=1.0)
    assert place.length == pytest.approx(3)
    assert place.ratio == pytest.approx(0.01)
    assert relative_differences(slab, s, limit=1.0, within_a_wall_from=1.0)[0].ratio == (
        pytest.approx(0.03)
    )


def test_neighbouring_structures_are_compared_however_near():
    slab = built(walls=[[(5, 1), (6, 1)], [(7, 1), (8, 1)]])
    s = np.zeros(len(slab.nodes))
    s[slab.xy[:, 0] >= 7] = 0.01
    (place,) = relative_differences(slab, s, limit=1.0)
    assert place.length == pytest.approx(1)
    assert place.ratio == pytest.approx(0.01)
    assert {place.a.x, place.b.x} == {6.0, 7.0}


def test_a_slab_rising_over_the_limit_is_over_it_too():
    """The limit is on the size, whichever way the slab moves."""
    slab = foundation([])
    s = np.full(len(slab.nodes), 0.2)
    s[0] = -0.01
    (zone,) = zones_over(slab, s, SETTLED)
    assert s[zone] == 0.2
    # The zone furthest from zero comes first, whichever way it went.
    s = np.zeros(len(slab.nodes))
    up, down = int(np.argmin(slab.xy[:, 0])), int(np.argmax(slab.xy[:, 0]))
    s[up], s[down] = 0.3, -0.2
    assert zones_over(slab, s, SETTLED) == [up, down]


def test_the_zones_over_the_limit_are_separate_with_their_deepest_nodes():
    slab = foundation([])
    x = slab.xy[:, 0]
    s = -0.1 - 0.06 * np.exp(-((x - 4) ** 2)) - 0.08 * np.exp(-((x - 20) ** 2))
    zones = zones_over(slab, s, SETTLED)
    assert [round(float(slab.xy[i, 0])) for i in zones] == [20, 4]
    assert zones_over(slab, s * 0, SETTLED) == []


def test_a_difference_is_the_same_in_either_sign():
    slab = foundation([(0, 0), (6, 0)])
    w = -0.001 * slab.xy[:, 0]
    (place,) = relative_differences(slab, w, RELATIVE)
    assert place.ratio == pytest.approx(0.001)
    assert sorted((place.a.settlement, place.b.settlement)) == pytest.approx([-0.006, 0.0])


def test_without_limits_nothing_is_over_and_the_largest_difference_is_shown():
    slab = foundation([(0, 0), (6, 0), (12, 0)])
    s = 0.01 * slab.xy[:, 0]  # 0.01 along X: over any usual limit
    assert zones_over(slab, -s) == []
    (place,) = relative_differences(slab, s)
    assert place.ratio == pytest.approx(0.01)
