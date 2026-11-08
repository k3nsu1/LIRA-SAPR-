"""Deflection relative to supports on slabs with a known answer, and the project limit."""

import math

import numpy as np
import pytest

from lira_sapr_mcp.postprocessing.deflection import (
    _through_a_support,
    allowed_deflections,
    panel_lows,
    worst_spans,
)
from lira_sapr_mcp.postprocessing.deflection import (
    relative_deflections as _relative_deflections,
)
from lira_sapr_mcp.postprocessing.mesh import find_slabs
from test_slab_mesh import grid

# A table a project may adopt as its limit (span m -> span / n, linear between the points): the
# logic of places over the limit is tested with it; the server itself has no built-in table.
TABLE = ((1.0, 120), (3.0, 150), (6.0, 200), (12.0, 250), (24.0, 300))


def table_limit(spans):
    ls = np.array([p[0] for p in TABLE])
    fs = ls / np.array([p[1] for p in TABLE])
    out = np.interp(spans, ls, fs)
    out = np.where(spans < ls[0], spans / TABLE[0][1], out)
    return np.where(spans > ls[-1], spans / TABLE[-1][1], out)


def relative_deflections(slab, w, fraction=table_limit):
    return _relative_deflections(slab, w, fraction)


def node_at(slab, x, y):
    return int(np.argmin(np.hypot(slab.xy[:, 0] - x, slab.xy[:, 1] - y)))


def line_between(slab, d, p, q):
    """The reading of the line between two points of supports."""
    a, b = node_at(slab, *p), node_at(slab, *q)
    lines = d.lines
    found = np.flatnonzero(((lines.a == a) & (lines.b == b)) | ((lines.a == b) & (lines.b == a)))
    return int(found[0]) if len(found) else None


def ends(slab, d):
    """The set of end pairs of the lines between supports, as coordinates."""
    lines = d.lines
    return {
        frozenset((tuple(slab.xy[a]), tuple(slab.xy[b])))
        for a, b in zip(lines.a.tolist(), lines.b.tolist(), strict=True)
        if b >= 0
    }


def test_allowed_deflection_is_the_project_fraction_of_the_span_or_the_span():
    spans = np.array([0.5, 6.0, 24.0])
    assert allowed_deflections(spans, 250) == pytest.approx(spans / 250)
    assert allowed_deflections(spans) == pytest.approx(spans)  # no limit: |f| / allowed is f / l
    assert allowed_deflections(np.array([4.5]), table_limit)[0] == pytest.approx(0.025)


def test_without_a_limit_nothing_is_over_it_and_the_worst_is_the_largest_f_to_l():
    slab, w = beam_slab(sag=0.03)
    d = _relative_deflections(slab, w)
    i = d.worst()
    assert d.lines.ratio[i] == pytest.approx(0.03 / 6, rel=1e-3) and d.lines.ratio.max() < 1
    assert worst_spans(d, 3) and all(d.lines.ratio[k] <= 1 for k in worst_spans(d, 3))


def beam_slab(shortening=0.0, sag=0.012):
    """6 m (X) by 12 m slab on walls along x = 0 and x = 6, mesh 0.5 m; the walls
    shorten by ``shortening`` and the slab sags as a simple beam across."""
    nodes, elements, number = grid(12, 24, size=0.5, z=3.0)
    held = {number[i, j] for i in (0, 12) for j in range(25)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    x = slab.xy[:, 0]
    w = -shortening - sag * np.sin(np.pi * x / 6)
    return slab, w


def test_beam_slab_midspan_deflection_is_the_sag_over_the_6m_span():
    slab, w = beam_slab()
    d = relative_deflections(slab, w)
    i = d.worst()
    lines = d.lines
    assert lines.f[i] == pytest.approx(-0.012)
    assert lines.span[i] == pytest.approx(6)
    assert lines.allowed[i] == pytest.approx(0.03)
    assert lines.ratio[i] == pytest.approx(0.4)
    assert lines.angle(slab, i) == 0
    assert lines.b[i] >= 0
    assert lines.point[i][0] == pytest.approx(3)
    # the limit at a node comes from the line passing by it
    mid = node_at(slab, 3, 6)
    assert d.f[mid] == pytest.approx(-0.012)
    assert d.span[mid] == pytest.approx(6)
    assert d.ratio[mid] == pytest.approx(0.4)
    assert np.isnan(d.f[node_at(slab, 0, 6)])  # a support
    # along the free edge too: the line runs along the slab edge
    edge = line_between(slab, d, (0, 0), (6, 0))
    assert lines.f[edge] == pytest.approx(-0.012)
    assert lines.span[edge] == pytest.approx(6)


def test_shortening_of_supports_is_not_deflection():
    slab, w = beam_slab(shortening=0.005)
    d = relative_deflections(slab, w)
    mid = node_at(slab, 3, 6)
    assert -w[mid] == pytest.approx(0.017)
    assert d.lines.f[d.worst()] == pytest.approx(-0.012)
    assert d.lines.w_line[d.worst()] == pytest.approx(-0.005)


def test_a_slope_between_the_supports_is_not_deflection():
    slab, _ = beam_slab(sag=0.0)
    w = -0.01 * slab.xy[:, 0] / 6  # one wall settles 10 mm, the slab stays straight
    d = relative_deflections(slab, w)
    assert np.abs(d.lines.f).max() < 1e-9


def four_columns():
    nodes, elements, number = grid(12, 12, size=0.5)
    corners = {number[i, j] for i in (0, 12) for j in (0, 12)}
    (slab,) = find_slabs(nodes, elements, restrained=corners)
    x, y = slab.xy[:, 0], slab.xy[:, 1]
    return slab, -0.02 * np.sin(np.pi * x / 6) * np.sin(np.pi * y / 6)


def test_slab_on_four_columns_is_read_along_the_six_lines_between_them():
    slab, w = four_columns()
    d = relative_deflections(slab, w)
    spans = d.lines.b >= 0
    assert spans.sum() == 6  # four sides and two diagonals
    i = d.worst()
    diagonal = 6 * math.sqrt(2)
    assert d.lines.f[i] == pytest.approx(-0.02)
    assert d.lines.span[i] == pytest.approx(diagonal)
    assert d.lines.angle(slab, i) in (45, 135)
    assert d.lines.allowed[i] == pytest.approx(0.03 + 0.018 * (diagonal - 6) / 6)
    assert d.lines.point[i] == pytest.approx((3, 3))
    assert not (d.lines.b < 0).any()  # every free edge has a line along it


def test_the_low_point_off_every_line_is_not_read_as_a_deflection():
    # The largest displacement is off both diagonals and the sides: no line may report it,
    # a line reads only its own points (the method was wrong about that once: it carried
    # the node over to the line).
    slab, w = four_columns()
    p = node_at(slab, 1.5, 3)
    w = np.zeros_like(w)
    w[p] = -0.05
    d = relative_deflections(slab, w)
    assert d.lines.f.min() == 0
    assert panel_lows(slab, w) == [p]
    # The isofield still shows it, under the limit of the nearest node on a line.
    assert d.f[p] == pytest.approx(-0.05)
    assert np.isfinite(d.ratio[p]) and d.span[p] >= 6


def test_a_point_between_nodes_is_interpolated_within_the_element():
    # Columns at (0, 0) and (3, 1) of a 0.5 m mesh: the line runs through elements.
    nodes, elements, number = grid(6, 2, size=0.5)
    (slab,) = find_slabs(nodes, elements, restrained={number[0, 0], number[6, 2]})
    w = 0.002 * slab.xy[:, 0] + 0.001 * slab.xy[:, 1]
    d = relative_deflections(slab, w)
    between = d.lines.b >= 0
    assert np.abs(d.lines.f[between]).max() < 1e-9  # a plane is read as the plane it is
    assert np.abs(d.lines.w_point[between] - d.lines.w_line[between]).max() < 1e-9
    # A dip of 6 mm at the node (1.5, 0.5) fades linearly to nothing at x = 1, through the element
    # the line crosses: the line's point nearest to the node reads its share at that x.
    w = np.zeros(len(slab.nodes))
    w[node_at(slab, 1.5, 0.5)] = -0.006
    d = relative_deflections(slab, w)
    i = line_between(slab, d, (0, 0), (3, 1))
    x, y = d.lines.point[i]
    assert y == pytest.approx(x / 3)  # on the line, between the nodes
    assert x != pytest.approx(1.5)
    assert d.lines.f[i] == pytest.approx(-0.006 * (1.5 - abs(x - 1.5) - 1) / 0.5)


def test_a_mesh_with_a_doubled_plate_and_a_collapsed_one_is_still_read():
    nodes, elements, number = grid(12, 4, size=0.5)
    held = {number[i, j] for i in (0, 12) for j in range(5)}
    elements[9001] = elements[1]  # the same plate twice
    nodes[9001] = (1.0, 0.0, 0.0)  # another node at a place taken: a seam
    nodes[9002] = (1.0, -1.0, 0.0)
    nodes[9003] = (1.0, -2.0, 0.0)
    elements[9002] = (44, (number[2, 0], 9001, 9002, 9003))  # all its nodes in one line
    (slab,) = find_slabs(nodes, elements, restrained=held)
    w = -0.01 * np.sin(np.pi * np.clip(slab.xy[:, 0], 0, 6) / 6)
    d = relative_deflections(slab, w)  # no exact answer on such a mesh, but no failure either
    assert -0.01 <= d.lines.f[d.worst()] < -0.005


def test_a_line_between_the_nodes_of_a_coarser_wall_does_not_cross_it():
    # A wall at x = 3 going down, its plates 1 m long, on a slab mesh of 0.5 m; a line at y = 0.5
    # passes between two wall nodes, 0.5 m from each.
    nodes, elements, number = grid(12, 4, size=0.5)
    for j in (0, 2, 4):
        nodes[8000 + j] = (3.0, j * 0.5, -3.0)
    for k, j in enumerate((0, 2)):
        tops = (number[6, j], number[6, j + 2])
        elements[8000 + k] = (44, (*tops, 8000 + j + 2, 8000 + j))
    held = {number[0, 1], number[12, 1]}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    assert len(slab.wall_lines) == 2
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert line_between(slab, d, (0, 0.5), (6, 0.5)) is None
    assert line_between(slab, d, (0, 0.5), (3, 0)) is not None


def test_a_slab_held_at_most_nodes_is_not_searched_for_spans():
    nodes, elements, number = grid(40, 40, size=0.25)
    (slab,) = find_slabs(nodes, elements, restrained=set(number.values()))
    assert slab.held_all_over and not slab.on_foundation
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert d.worst() is None


def test_a_line_does_not_pass_through_another_support_or_across_a_wall():
    # Columns in a row at x = 0, 3, 6 and a wall across at y = 3 between (3, 3)...(3, 5).
    nodes, elements, number = grid(12, 12, size=0.5)
    row = {number[i, 0] for i in (0, 6, 12)}
    wall = {number[6, j] for j in range(6, 11)}
    far = {number[0, 8], number[12, 8]}
    (slab,) = find_slabs(nodes, elements, restrained=row | wall | far)
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    pairs = ends(slab, d)

    def pair(p, q):
        return frozenset((p, q))

    assert pair((0.0, 0.0), (3.0, 0.0)) in pairs
    assert pair((0.0, 0.0), (6.0, 0.0)) not in pairs  # through the middle column
    assert pair((0.0, 4.0), (6.0, 4.0)) not in pairs  # across the wall
    assert pair((0.0, 4.0), (3.0, 4.0)) in pairs  # up to the wall
    assert pair((3.0, 3.0), (3.0, 3.5)) not in pairs  # along the wall: it is one support
    assert pair((3.0, 3.0), (3.0, 5.0)) not in pairs


def test_the_quick_check_of_supports_on_a_line_finds_what_a_plain_search_finds():
    rng = np.random.default_rng(5)
    pts = rng.uniform(0, 30, (300, 2))
    pts[:60, 1] = 7.5  # supports in a row: many lie on each other's lines
    origin = 3
    rel = pts - pts[origin]
    distance = np.hypot(rel[:, 0], rel[:, 1])
    targets = np.setdiff1d(np.arange(len(pts)), [origin])
    quick = _through_a_support(rel, distance, targets)
    for row, j in enumerate(targets):
        length = distance[j]
        along = (rel * rel[j]).sum(axis=1) / length
        off = np.abs(rel[:, 0] * rel[j, 1] - rel[:, 1] * rel[j, 0]) / length
        plain = ((off < 0.1) & (along > 0.1) & (along < length - 0.1)).any()
        assert quick[row] == plain, j
    assert quick.any() and not quick.all()


def test_cantilever_spans_twice_the_overhang():
    nodes, elements, number = grid(6, 4, size=0.5)  # 3 m out from a wall at x = 0
    (slab,) = find_slabs(nodes, elements, restrained={number[0, j] for j in range(5)})
    x = slab.xy[:, 0]
    w = -0.006 * (x / 3) ** 2
    d = relative_deflections(slab, w)
    lines = d.lines
    assert (lines.b < 0).all()
    i = d.worst()
    assert lines.f[i] == pytest.approx(-0.006)
    assert lines.span[i] == pytest.approx(6)
    assert lines.ratio[i] == pytest.approx(0.2)
    assert lines.point[i][0] == pytest.approx(3)  # at the free edge
    assert lines.end[i][0] == pytest.approx(3)
    assert lines.w_line[i] == pytest.approx(0)
    tip = node_at(slab, 3, 1)
    assert d.span[tip] == pytest.approx(6)
    assert d.f[tip] == pytest.approx(-0.006)


def opening_slab():
    # Walls at x = 0 and x = 6; an opening between x = 2 and 4 for y 1..3.
    skip = {(i, j) for i in range(4, 8) for j in range(2, 6)}
    nodes, elements, number = grid(12, 8, size=0.5, skip=skip)
    held = {number[i, j] for i in (0, 12) for j in range(9)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    return slab


def test_edge_of_an_opening_stops_a_line():
    slab = opening_slab()
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert line_between(slab, d, (0, 2), (6, 2)) is None
    assert line_between(slab, d, (0, 0.5), (6, 0.5)) is not None


def test_the_free_edge_of_an_opening_overhangs_when_no_line_comes_near():
    slab = opening_slab()
    w = np.zeros(len(slab.nodes))
    p = node_at(slab, 2, 2)  # on the edge of the opening, 2 m from the wall straight across
    w[p] = -0.004
    d = relative_deflections(slab, w)
    # Slanting lines pass around the opening's corners, but none reaches the middle of its edge.
    assert d.span[p] == pytest.approx(4)
    assert d.f[p] == pytest.approx(-0.004)


def test_a_line_along_the_edge_of_an_opening_is_not_stopped():
    slab = opening_slab()
    w = np.zeros(len(slab.nodes))
    p = node_at(slab, 1.5, 1)
    w[p] = -0.003
    d = relative_deflections(slab, w)
    i = line_between(slab, d, (0, 1), (6, 1))
    assert i is not None
    assert d.lines.span[i] == pytest.approx(6)
    assert d.lines.f[i] == pytest.approx(-0.003)
    assert d.lines.point[i] == pytest.approx((1.5, 1))


def test_a_line_does_not_cut_across_a_notch():
    # L-shaped slab: the notch is x > 2, y > 2 of a 4 x 4 square, supports at the tips of the
    # two legs: the straight line between the tips runs outside.
    skip = {(i, j) for i in range(2, 4) for j in range(2, 4)}
    nodes, elements, number = grid(4, 4, skip=skip)
    tips = {number[4, 2], number[2, 4]}
    (slab,) = find_slabs(nodes, elements, restrained=tips)
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert line_between(slab, d, (4, 2), (2, 4)) is None


def test_slab_without_supports_measures_nothing():
    nodes, elements, _ = grid(2, 2)
    (slab,) = find_slabs(nodes, elements)
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert d.worst() is None


def test_a_slab_on_springs_at_most_nodes_lies_on_a_foundation_and_measures_nothing():
    nodes, elements, number = grid(2, 2)  # 9 nodes
    springs = {100 + k: (51, (n,)) for k, n in enumerate(number.values())}
    (on_ground,) = find_slabs(nodes, elements | springs)
    assert on_ground.on_foundation and on_ground.on_soil and on_ground.springs == 9
    d = relative_deflections(on_ground, np.arange(9) * -0.001)
    assert d.worst() is None


@pytest.mark.parametrize("one_node_type", [51, 56, 57, 251])
def test_a_slab_on_springs_under_separate_points_settles_too_and_measures_nothing(one_node_type):
    """Whatever the one-node element: a support, a link with a given stiffness or a pile."""
    nodes, elements, number = grid(6, 6, size=1.0)
    piles = {100 + k: (one_node_type, (number[i, j],)) for k, (i, j) in enumerate(
        (i, j) for i in (0, 3, 6) for j in (0, 3, 6)
    )}  # fmt: skip
    (slab,) = find_slabs(nodes, elements | piles)
    assert slab.on_foundation and not slab.on_soil and slab.springs == 9
    assert relative_deflections(slab, slab.xy[:, 0] * -0.001).worst() is None
    # On a pile at one point only, too.
    (slab,) = find_slabs(nodes, elements | {100: (one_node_type, (number[3, 3],))})
    assert slab.on_foundation


def piles_under_rigid_bodies(head_in_slab=True):
    """A slab on 4 piles, each under a rigid body of 9 slab nodes, with the pile's head either at
    the middle slab node or at a node of its own off the slab."""
    nodes, elements, number = grid(24, 24, size=1.0)
    piles, groups = {}, []
    for k, (i, j) in enumerate((i, j) for i in (6, 18) for j in (6, 18)):
        around = {number[i + a, j + b] for a in (-1, 0, 1) for b in (-1, 0, 1)}
        head = number[i, j]
        if not head_in_slab:
            head = 9000 + k
            nodes[head] = (*nodes[number[i, j]][:2], -1.0)
        piles[8000 + k] = (57, (head,))
        groups.append(around | {head})
    return nodes, elements | piles, groups


@pytest.mark.parametrize("head_in_slab", [True, False])
def test_a_slab_on_piles_under_rigid_bodies_settles_like_one_on_the_piles_alone(head_in_slab):
    nodes, elements, groups = piles_under_rigid_bodies(head_in_slab)
    (slab,) = find_slabs(nodes, elements, groups=groups)
    assert slab.on_foundation and not slab.on_soil
    assert slab.springs == 36 and slab.springs_through_bodies == (32 if head_in_slab else 36)
    assert (slab.points_on_springs, slab.points) == (4, 4)
    assert relative_deflections(slab, slab.xy[:, 0] * -0.001).worst() is None


@pytest.mark.parametrize(("columns", "under_bodies"), [(4, 1), (16, 3)])
def test_a_rigid_body_counts_as_one_support_among_columns(columns, under_bodies):
    """A spring under a body of 9 nodes must not outweigh columns standing on one node each."""
    nodes, elements, number = grid(24, 24, size=1.0)
    more, groups = {}, []
    for k in range(columns):
        n = number[k + 1, 1]
        nodes[9000 + k] = (*nodes[n][:2], -3.0)
        more[8000 + k] = (10, (9000 + k, n))
    for k in range(under_bodies):
        i, j = 4 + 6 * k, 12
        more[7000 + k] = (51, (number[i, j],))
        groups.append({number[i + a, j + b] for a in (-1, 0, 1) for b in (-1, 0, 1)})
    (slab,) = find_slabs(nodes, elements | more, groups=groups)
    assert slab.springs == 9 * under_bodies
    assert (slab.points_on_springs, slab.points) == (under_bodies, columns + under_bodies)
    assert not slab.on_foundation


def one_column_and_a_body_on_a_spring():
    nodes, elements, number = grid(6, 6, size=1.0)
    nodes[900] = (*nodes[number[1, 1]][:2], -3.0)
    below = {800: (10, (900, number[1, 1])), 801: (51, (number[4, 4],))}
    body = {number[4 + a, 4 + b] for a in (-1, 0, 1) for b in (-1, 0, 1)}
    return nodes, elements | below, body, number


def test_a_rigid_body_repeated_as_a_coupled_group_is_one_support():
    nodes, elements, body, _ = one_column_and_a_body_on_a_spring()
    (once,) = find_slabs(nodes, elements, groups=[body])
    (twice,) = find_slabs(nodes, elements, groups=[body, set(body)])
    assert (once.points_on_springs, once.points) == (1, 2)
    assert (twice.points_on_springs, twice.points) == (1, 2)
    assert not once.on_foundation and not twice.on_foundation


def test_bodies_inside_a_body_on_a_column_are_on_that_column():
    nodes, elements, body, number = one_column_and_a_body_on_a_spring()
    a, b = sorted(body)[:2], sorted(body)[2:4]
    spring_a = {700: (51, (a[0],)), 701: (51, (b[0],))}
    outer = body | {number[1, 1]}  # the column holds the whole of it
    (slab,) = find_slabs(nodes, elements | spring_a, groups=[set(a), set(b), outer])
    assert (slab.points_on_springs, slab.points) == (0, 1)
    assert not slab.on_foundation


@pytest.mark.parametrize("spring_body_first", [True, False])
def test_a_node_of_two_rigid_bodies_does_not_depend_on_their_order(spring_body_first):
    nodes, elements, number = grid(6, 6, size=1.0)
    nodes[900] = (*nodes[number[1, 1]][:2], -3.0)
    below = {800: (10, (900, number[1, 1])), 801: (51, (number[3, 1],))}
    on_column = {number[1, 1], number[2, 1]}
    on_spring = {number[2, 1], number[3, 1]}  # shares the node between them
    groups = [on_spring, on_column] if spring_body_first else [on_column, on_spring]
    (slab,) = find_slabs(nodes, elements | below, groups=groups)
    assert slab.support_kinds[int(np.flatnonzero(slab.nodes == number[2, 1])[0])] == "column"
    assert slab.springs == 1


def test_a_rigid_body_on_a_column_and_a_spring_is_held_by_the_column():
    nodes, elements, number = grid(6, 6, size=1.0)
    nodes[900] = (*nodes[number[3, 3]][:2], -3.0)
    below = {800: (10, (900, number[3, 3])), 801: (51, (number[3, 4],))}
    body = {number[3, 3], number[3, 4], number[4, 3], number[4, 4]}
    (slab,) = find_slabs(nodes, elements | below, groups=[body])
    # Only the spring's own node stays on a spring.
    assert slab.springs == 1 and slab.springs_through_bodies == 0 and not slab.on_foundation


@pytest.mark.parametrize("spring_first", [True, False])
def test_the_kind_of_a_node_does_not_depend_on_the_order_of_the_elements(spring_first):
    """A node with both a spring and a bar going down is held by the bar, whichever comes first."""
    nodes, elements, number = grid(2, 2)
    springs, bars = {}, {}
    for k, n in enumerate(number.values()):
        nodes[900 + k] = (*nodes[n][:2], -3.0)
        springs[(100 if spring_first else 200) + k] = (51, (n,))
        bars[(200 if spring_first else 100) + k] = (10, (900 + k, n))
    together = dict(sorted({**springs, **bars}.items()))
    (slab,) = find_slabs(nodes, elements | together)
    assert slab.springs == 0 and not slab.on_foundation


def test_a_slab_with_bedding_on_most_of_its_plates_settles_and_measures_nothing():
    nodes, elements, number = grid(4, 4, size=1.0)
    plates = sorted(elements)
    (slab,) = find_slabs(nodes, elements, bedded=set(plates[:9]))
    assert slab.bedded == 9 and slab.on_foundation and slab.on_soil
    assert not slab.springs_all_over and len(slab.supports) == 0
    assert relative_deflections(slab, slab.xy[:, 0] * -0.001).worst() is None
    # A strip of bedding under a slab on restraints is not a foundation.
    (slab,) = find_slabs(
        nodes, elements, restrained={number[0, 0], number[4, 4]}, bedded={plates[0]}
    )
    assert slab.bedded == 1 and not slab.on_foundation


def test_a_slab_on_rigid_points_is_not_a_slab_on_springs():
    nodes, elements, number = grid(6, 6, size=1.0)
    (slab,) = find_slabs(nodes, elements, restrained={number[0, 0], number[6, 6], number[0, 6]})
    assert slab.springs == 0 and not slab.on_foundation


def test_a_floor_on_columns_with_a_spring_among_them_still_spans():
    nodes, elements, number = grid(6, 6, size=1.0)
    corners = [(0, 0), (6, 0), (0, 6), (6, 6)]
    more = {}
    for k, (i, j) in enumerate(corners):
        x, y, _ = nodes[number[i, j]]
        nodes[900 + k] = (x, y, -3.0)
        more[800 + k] = (10, (900 + k, number[i, j]))
    (slab,) = find_slabs(nodes, elements | more | {700: (51, (number[3, 3],))})
    assert slab.springs == 1 and not slab.on_foundation
    # As many springs as other supports: not most of them.
    springs = {700 + k: (51, (number[i, 3],)) for k, i in enumerate((1, 2, 3, 4))}
    (slab,) = find_slabs(nodes, elements | more | springs)
    assert slab.springs == 4 and not slab.on_foundation
    assert relative_deflections(slab, slab.xy[:, 0] * -0.001).lines.f.size


def three_walls(first, second):
    """Walls at x = 0, 6 and 12: the first span sags ``first``, the second ``second``."""
    nodes, elements, number = grid(24, 6, size=0.5, z=3.0)
    held = {number[i, j] for i in (0, 12, 24) for j in range(7)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    x = slab.xy[:, 0]
    w = np.where(x <= 6, -first * np.sin(np.pi * x / 6), -second * np.sin(np.pi * (x - 6) / 6))
    return slab, w


def test_worst_spans_are_one_per_place_the_worst_first():
    slab, w = three_walls(0.012, 0.006)
    d = relative_deflections(slab, w)
    spans = worst_spans(d)
    assert [round(float(d.lines.point[i, 0])) for i in spans[:2]] == [3, 9]
    assert d.lines.ratio[spans[0]] == pytest.approx(0.4)
    assert d.lines.ratio[spans[1]] == pytest.approx(0.2)
    assert spans[0] == d.worst()
    assert worst_spans(d, count=1) == spans[:1]
    assert worst_spans(relative_deflections(slab, w * 0)) != []  # no sag: still listed
    (free,) = find_slabs(*grid(24, 6, size=0.5, z=3.0)[:2])
    assert worst_spans(relative_deflections(free, np.zeros(len(free.nodes)))) == []


def test_every_place_over_the_limit_is_shown_even_past_three():
    # Eight walls make seven spans of 3 m (limit 20 mm), all sagging 25 mm.
    nodes, elements, number = grid(42, 4, size=0.5)
    held = {number[i, j] for i in range(0, 43, 6) for j in range(5)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    x = slab.xy[:, 0]
    w = -0.025 * np.abs(np.sin(np.pi * x / 3))
    d = relative_deflections(slab, w)
    shown = worst_spans(d)
    assert all(d.lines.ratio[i] > 1 for i in shown)
    assert {int(d.lines.point[i, 0] // 3) for i in shown} == set(range(7))  # all seven bays
    assert len(shown) >= 7


def test_panel_lows_are_the_bottom_of_each_hollow():
    slab, w = three_walls(0.012, 0.006)
    lows = panel_lows(slab, w)
    assert len(lows) == 2
    assert slab.xy[lows[0], 0] == pytest.approx(3)
    assert slab.xy[lows[1], 0] == pytest.approx(9)
    assert w[lows[0]] == pytest.approx(-0.012)
    # a dent shallower than a millimetre is not a panel of its own
    nodes, elements, number = grid(12, 4, size=0.5)
    (flat,) = find_slabs(nodes, elements, restrained={number[0, 0], number[12, 4]})
    w = -0.01 * np.sin(np.pi * flat.xy[:, 0] / 6)
    w[node_at(flat, 1, 1)] -= 0.0001
    assert len(panel_lows(flat, w)) == 1
    assert flat.xy[panel_lows(flat, w)[0], 0] == pytest.approx(3)
    # the nodes of supports do not count
    w = np.ones(len(flat.nodes)) * -0.001
    w[node_at(flat, 0, 0)] = -0.05
    assert node_at(flat, 0, 0) not in panel_lows(flat, w)


def test_a_short_span_over_the_limit_beside_a_longer_one_is_a_place_of_its_own():
    # Walls at x = 0, 6 and 9 on a slab 2 m wide: the 6 m bay is over its limit (30 mm) by 1.3,
    # the 3 m bay over its own (20 mm) by 1.1; their middles are 4.5 m apart, less than 0.9 x 6.
    nodes, elements, number = grid(18, 4, size=0.5)
    held = {number[i, j] for i in (0, 12, 18) for j in range(5)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    x = slab.xy[:, 0]
    w = np.where(x <= 6, -0.039 * np.sin(np.pi * x / 6), -0.022 * np.sin(np.pi * (x - 6) / 3))
    d = relative_deflections(slab, w)
    bays = {int(d.lines.point[i, 0] // 6) for i in worst_spans(d) if d.lines.ratio[i] > 1}
    assert bays == {0, 1}


def test_a_flat_slab_has_no_panels():
    slab, w = three_walls(0.0, 0.0)
    assert panel_lows(slab, w) == []


def test_a_cantilever_is_the_shortest_way_from_the_edge_to_a_column():
    # A row of columns at x = 3, every 4 m, and a free edge at x = 4: an overhang of 1 m. The edge
    # between the columns is further from them, but that does not make the overhang longer. A wall
    # at x = 0 closes the slab on the other side.
    nodes, elements, number = grid(8, 24, size=0.5)
    held = {number[6, j] for j in range(0, 25, 8)} | {number[0, j] for j in range(25)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    x = slab.xy[:, 0]
    w = -0.004 * np.where(x > 3, (x - 3), 0.0)  # bends past the columns
    d = relative_deflections(slab, w)
    lines = d.lines
    overhangs = np.flatnonzero((lines.b < 0) & (lines.end[:, 0] == 4))
    assert len(overhangs) == 2  # at the columns in the middle; those at the corners have the side
    assert lines.span[overhangs] == pytest.approx(2.0)  # twice 1 m, never more
    # at the end, on the edge, level with its column, relative to it
    assert (lines.point[overhangs] == lines.end[overhangs]).all()
    assert slab.xy[lines.a[overhangs], 1] == pytest.approx(lines.end[overhangs, 1])
    assert lines.f[overhangs] == pytest.approx(-0.004)


def test_a_cantilever_is_read_at_its_end_even_if_it_sags_more_on_the_way():
    nodes, elements, number = grid(6, 2, size=0.5)  # 3 m out from a wall at x = 0
    (slab,) = find_slabs(nodes, elements, restrained={number[0, j] for j in range(3)})
    x = slab.xy[:, 0]
    w = -0.01 * np.sin(np.pi * x / 3)  # back at the level of the wall at the end
    d = relative_deflections(slab, w)
    lines = d.lines
    assert (lines.point[:, 0] == 3).all()
    assert np.abs(lines.f).max() < 1e-9


def test_a_free_edge_beside_a_support_on_the_edge_is_no_overhang():
    # Columns on the edge y = 0 at x = 0 and 3 and a wall at y = 3: the edge has a line along it.
    nodes, elements, number = grid(6, 6, size=0.5)
    held = {number[0, 0], number[6, 0]} | {number[i, 6] for i in range(7)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    d = relative_deflections(slab, np.zeros(len(slab.nodes)))
    assert not (d.lines.b < 0).any()


def many_spans(overhang=3):
    """Points held in Z every 6 m in a 1 m mesh, 3 x 2 spans, with ``overhang`` m out all round."""
    nodes, elements, number = grid(18 + 2 * overhang, 12 + 2 * overhang, size=1.0)
    held = {number[overhang + 6 * a, overhang + 6 * b] for a in range(4) for b in range(3)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    return slab


def wavy(slab):
    x, y = slab.xy.T
    return 0.001 * np.sin(x * 0.7) * np.cos(y * 0.9) - 0.002


def test_with_every_support_at_zero_the_field_is_the_displacement_in_every_node():
    # The owner's check: on rigid supports nothing is left but LIRA's own displacement.
    slab = many_spans()
    w = wavy(slab)
    w[slab.supports] = 0.0
    d = relative_deflections(slab, w)
    free = np.ones(len(w), dtype=bool)
    free[slab.supports] = False
    assert np.isfinite(d.f[free]).all()  # no white on the picture
    assert d.f[free] == pytest.approx(w[free], abs=1e-12)
    assert np.isnan(d.f[slab.supports]).all()


def test_every_node_has_a_limit_and_a_ratio():
    slab = many_spans()
    d = relative_deflections(slab, wavy(slab))
    free = np.ones(len(slab.nodes), dtype=bool)
    free[slab.supports] = False
    assert np.isfinite(d.ratio[free]).all() and np.isfinite(d.allowed[free]).all()


def test_a_plane_through_the_supports_is_no_deflection_anywhere():
    slab = many_spans()
    x, y = slab.xy.T
    w = 0.002 * x - 0.001 * y - 0.01
    d = relative_deflections(slab, w)
    within = (x >= 3) & (x <= 21) & (y >= 3) & (y <= 15)
    assert np.nanmax(np.abs(d.f[within])) < 1e-9
    # Beyond the outer supports the surface stays at the straight line between the two nearest,
    # as an overhang stays at its support: a tilt of the whole slab reads there.
    assert d.f[node_at(slab, 12, 0)] == pytest.approx(0.003)


def test_beyond_the_outer_supports_the_field_reads_from_the_line_between_the_two_nearest():
    slab = many_spans()
    w = np.zeros(len(slab.nodes))
    i, j = node_at(slab, 3, 3), node_at(slab, 9, 3)  # two supports of the outer row, 6 m apart
    w[j] = -0.006
    d = relative_deflections(slab, w)
    # 3 m out from the middle of them: the line there has come down by half, the slab has not.
    assert d.f[node_at(slab, 6, 0)] == pytest.approx(0.003)
    assert d.f[i] != d.f[i]  # NaN at a support


def test_supports_in_one_line_have_only_that_line():
    nodes, elements, number = grid(12, 4, size=1.0)
    (slab,) = find_slabs(nodes, elements, restrained={number[i, 2] for i in (0, 6, 12)})
    w = np.zeros(len(slab.nodes))
    w[node_at(slab, 6, 2)] = -0.006
    d = relative_deflections(slab, w)
    assert d.f[node_at(slab, 3, 0)] == pytest.approx(0.003)
    assert np.isfinite(d.f[node_at(slab, 12, 4)])


def test_one_support_is_a_level_for_the_whole_slab():
    nodes, elements, number = grid(4, 4, size=1.0)
    (slab,) = find_slabs(nodes, elements, restrained={number[2, 2]})
    w = np.full(len(slab.nodes), -0.002)
    w[node_at(slab, 2, 2)] = -0.005
    assert relative_deflections(slab, w).f[node_at(slab, 0, 0)] == pytest.approx(0.003)


def test_a_node_no_line_passes_over_its_limit_is_a_place_of_its_own():
    slab = many_spans()
    w = np.zeros(len(slab.nodes))
    d = relative_deflections(slab, w)
    (corner,) = (i for i in range(len(w)) if tuple(slab.xy[i]) == (0.0, 0.0))
    assert not any((d.lines.end == slab.xy[corner]).all(axis=1))
    w[corner] = -0.5
    d = relative_deflections(slab, w)
    (i,) = np.flatnonzero((d.lines.end == slab.xy[corner]).all(axis=1))
    assert d.lines.b[i] == -1 and d.lines.f[i] == pytest.approx(-0.5)
    assert d.lines.ratio[i] > 1 and d.lines.ratio[i] == pytest.approx(d.ratio[corner])
    assert d.lines.span[i] == pytest.approx(2 * math.hypot(3, 3))
    assert tuple(slab.xy[d.lines.a[i]]) == (3.0, 3.0)
    assert d.worst() == i
