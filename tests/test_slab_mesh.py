"""Slabs in a mesh: level plates, outline with holes, supports from below."""

import numpy as np

from lira_sapr_mcp.postprocessing.mesh import find_slabs, outline_loops, polygon_area


def grid(nx, ny, size=1.0, z=0.0, first_node=1, first_el=1, skip=()):
    """A plan grid of nx x ny quad plates (FE 44) at level z; ``skip``: (i, j) cells left
    out (an opening). Returns nodes, elements and node number by (i, j)."""
    nodes, number = {}, {}
    k = first_node
    for j in range(ny + 1):
        for i in range(nx + 1):
            nodes[k] = (i * size, j * size, z)
            number[i, j] = k
            k += 1
    elements = {}
    e = first_el
    for j in range(ny):
        for i in range(nx):
            if (i, j) in skip:
                continue
            ns = (number[i, j], number[i + 1, j], number[i + 1, j + 1], number[i, j + 1])
            elements[e] = (44, ns)
            e += 1
    return nodes, elements, number


def area(slab):
    return sum(polygon_area(slab.xy[f]) for f in slab.faces)


def test_level_plates_connected_through_nodes_make_one_slab():
    nodes, elements, _ = grid(3, 2)
    (slab,) = find_slabs(nodes, elements)
    assert slab.number == 1
    assert slab.z == 0
    assert list(slab.elements) == list(range(1, 7))
    assert len(slab.nodes) == 12
    assert area(slab) == 6
    assert len(slab.triangles()) == 12


def test_two_levels_and_two_separate_parts_are_separate_slabs_lowest_first():
    n1, e1, _ = grid(2, 2, z=3.0)
    n2, e2, _ = grid(2, 2, z=0.0, first_node=100, first_el=100)
    n3, e3, _ = grid(1, 1, z=0.0, first_node=200, first_el=200)
    n3 = {k: (x + 10, y, z) for k, (x, y, z) in n3.items()}
    slabs = find_slabs(n1 | n2 | n3, e1 | e2 | e3)
    assert [(s.number, s.z, int(s.elements[0])) for s in slabs] == [
        (1, 0.0, 100),
        (2, 0.0, 200),
        (3, 3.0, 1),
    ]


def test_sloped_plates_plane_stress_plates_and_plates_outside_within_are_not_slabs():
    nodes, elements, number = grid(2, 1)
    nodes[number[2, 1]] = (2.0, 1.0, 0.5)  # lift a corner: plate 2 slopes
    elements[3] = (21, elements[1][1])  # plane stress twin of plate 1
    (slab,) = find_slabs(nodes, elements)
    assert list(slab.elements) == [1]
    assert find_slabs(nodes, elements, within={2, 3}) == []


def test_outline_includes_the_edges_of_an_opening():
    nodes, elements, _ = grid(3, 3, skip={(1, 1)})
    (slab,) = find_slabs(nodes, elements)
    assert len(slab.boundary) == 12 + 4
    assert slab.boundary_nodes.sum() == 12 + 4
    assert area(slab) == 8


def test_outline_loops_take_every_edge_of_openings_touching_at_a_corner():
    nodes, elements, _ = grid(4, 4, skip={(1, 1), (2, 2)})
    (slab,) = find_slabs(nodes, elements)
    loops = outline_loops(slab)
    sides = {
        tuple(sorted((tuple(a), tuple(b))))
        for loop in loops
        for a, b in zip(loop, np.roll(loop, -1, axis=0), strict=True)
    }
    assert sum(len(loop) for loop in loops) == len(sides) == 16 + 8
    assert sides == {tuple(sorted(map(tuple, s))) for s in slab.boundary}


def test_crosswise_listed_quad_is_put_in_order():
    nodes = {1: (0, 0, 0), 2: (1, 0, 0), 3: (1, 1, 0), 4: (0, 1, 0)}
    (slab,) = find_slabs(nodes, {1: (44, (1, 3, 2, 4))})
    assert area(slab) == 1


def test_supports_are_what_holds_the_slab_from_below():
    nodes, elements, number = grid(4, 1, z=3.0)
    k = max(nodes) + 1
    # a column under (0, 0), a column above (1, 0) - it loads the slab, a wall panel
    # under the edge (4, 0)-(4, 1), a restraint at (2, 1), a spring at (3, 1).
    nodes[k] = (0.0, 0.0, 0.0)
    elements[100] = (10, (k, number[0, 0]))
    nodes[k + 1] = (1.0, 0.0, 6.0)
    elements[101] = (10, (number[1, 0], k + 1))
    nodes[k + 2], nodes[k + 3] = (4.0, 0.0, 0.0), (4.0, 1.0, 0.0)
    elements[102] = (44, (k + 2, k + 3, number[4, 1], number[4, 0]))
    elements[103] = (51, (number[3, 1],))
    (slab,) = find_slabs(nodes, elements, restrained={number[2, 1]})
    kinds = {int(slab.nodes[i]): kind for i, kind in slab.support_kinds.items()}
    assert kinds == {
        number[0, 0]: "column",
        number[4, 0]: "wall",
        number[4, 1]: "wall",
        number[2, 1]: "restraint",
        number[3, 1]: "spring",
    }
    assert list(slab.supports) == sorted(slab.support_kinds)


def test_rigid_body_with_a_column_node_makes_its_slab_nodes_supports():
    nodes, elements, number = grid(2, 2, z=3.0)
    # The column top (node 50) is a master node off the mesh, tied to four slab nodes.
    nodes[50], nodes[51] = (1.0, 1.0, 3.0), (1.0, 1.0, 0.0)
    elements[100] = (10, (51, 50))
    tied = {50, number[1, 1], number[0, 1], number[1, 0]}
    (slab,) = find_slabs(nodes, elements, groups=[tied, {number[2, 2]}])
    held = {int(slab.nodes[i]) for i in slab.supports}
    assert held == tied - {50}


def test_slab_positions_are_plan_coordinates():
    nodes, elements, number = grid(1, 1, size=2.0, z=5.0)
    (slab,) = find_slabs(nodes, elements)
    i = int(np.where(slab.nodes == number[1, 1])[0][0])
    assert list(slab.xy[i]) == [2.0, 2.0]


def test_postprocessing_knows_neither_lira_nor_com():
    # Decision 0003: the module works on arrays and runs without LIRA.
    import ast
    from pathlib import Path

    import lira_sapr_mcp.postprocessing as package

    banned = ("win32", "pythoncom", "pywintypes", "lira_sapr_mcp.adapters", "lira_sapr_mcp.com",
              "lira_sapr_mcp.services", "lira_sapr_mcp.tools")  # fmt: skip
    for file in Path(package.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not [n for n in names if n.startswith(banned)], (file.name, names)


def test_what_stands_on_a_slab_is_vertical_and_goes_up():
    nodes, elements, number = grid(4, 4, size=1.0)
    extra = {
        7001: (2.0, 2.0, 3.0),  # above (2, 2): a column
        7002: (1.0, 3.0, 3.0),  # above (1, 3)...
        7003: (2.0, 3.0, 3.0),  # ...and (2, 3): a wall plate
        7004: (4.0, 4.0, 1.0),  # from (0, 0): a long strut, not a vertical structure
        7005: (3.0, 1.0, -3.0),  # below (3, 1): a support, not on the slab
        7006: (0.0, 2.0, 3.0),  # above (0, 2), through a rigid body with (0, 3)
    }
    more = {
        8001: (10, (number[2, 2], 7001)),
        8002: (44, (number[1, 3], number[2, 3], 7003, 7002)),
        8003: (10, (number[0, 0], 7004)),
        8004: (10, (number[3, 1], 7005)),
        8005: (10, (number[0, 2], 7006)),
        8006: (51, (number[4, 0],)),  # a one-node spring
        8007: (10, (number[4, 4], 999_999)),  # an unknown node
    }
    (slab, *_) = find_slabs(nodes | extra, elements | more, groups=[{number[0, 2], number[0, 3]}])
    at = {tuple(slab.xy[i]) for i in slab.carried}
    assert at == {(2.0, 2.0), (1.0, 3.0), (2.0, 3.0), (0.0, 2.0), (0.0, 3.0)}
    found = {frozenset(tuple(slab.xy[i]) for i in s.nodes): s.body for s in slab.structures}
    assert found == {
        frozenset({(2.0, 2.0)}): True,  # a column on its node
        frozenset({(0.0, 2.0), (0.0, 3.0)}): True,  # a column on a rigid body
        frozenset({(1.0, 3.0), (2.0, 3.0)}): False,  # a wall
    }


def test_plates_meeting_at_a_node_make_one_wall_with_a_rigid_body_they_take_in():
    nodes, elements, number = grid(6, 6)
    extra = {7000 + k: (*nodes[number[at]][:2], 3.0) for k, at in enumerate(
        [(1, 1), (2, 1), (2, 2), (5, 5)]
    )}  # fmt: skip
    more = {
        8001: (44, (number[1, 1], number[2, 1], 7001, 7000)),
        8002: (44, (number[2, 1], number[2, 2], 7002, 7001)),  # an L: shares (2, 1)
        8003: (10, (number[5, 5], 7003)),
    }
    (slab, *_) = find_slabs(nodes | extra, elements | more, groups=[{number[2, 2], number[3, 2]}])
    found = {frozenset(tuple(slab.xy[i]) for i in s.nodes): s.body for s in slab.structures}
    assert found == {
        frozenset({(1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (3.0, 2.0)}): False,
        frozenset({(5.0, 5.0)}): True,
    }


def two_levels():
    """Slab 1 at Z 0 and slab 2 at Z 3, each of 4 x 2 cells: elements 1-8 and 1001-1008."""
    nodes, elements, _ = grid(4, 2)
    upper, plates, _ = grid(4, 2, z=3.0, first_node=1001, first_el=1001)
    return nodes | upper, elements | plates


def test_a_chosen_slab_keeps_its_number_and_knows_the_plan():
    nodes, elements = two_levels()
    (slab,) = find_slabs(nodes, elements, within=set(range(1001, 1009)))
    assert slab.number == 2 and slab.z == 3.0
    assert not slab.part
    assert len(slab.site) == 24  # the outlines of both slabs
    assert slab.site is not None and len(slab.boundary) == 12


def test_part_of_a_slab_is_told_from_the_whole_and_scattered_plates_stay_one_part():
    nodes, elements = two_levels()
    (slab,) = find_slabs(nodes, elements, within={1001, 1004})  # opposite ends of the row
    assert slab.number == 2 and slab.part
    assert slab.whole_elements == 8 and list(slab.elements) == [1001, 1004]
    assert not next(s for s in find_slabs(nodes, elements) if s.number == 2).part


def test_plates_of_two_slabs_give_both_under_their_numbers():
    nodes, elements = two_levels()
    low, high = find_slabs(nodes, elements, within={1, 2, 3, 4, 5, 6, 7, 8, 1001})
    assert (low.number, low.part, high.number, high.part) == (1, False, 2, True)


def segments(array):
    return {frozenset(map(tuple, s.tolist())) for s in array}


def test_the_key_plan_has_the_outlines_the_slabs_have():
    # An opening, a triangle and a quad listed crosswise, as LIRA's text lists them.
    nodes, elements, number = grid(4, 3, skip={(1, 1)})
    upper, plates, top = grid(3, 2, z=3.0, first_node=1001, first_el=1001)
    plates[1001] = (44, (top[0, 0], top[1, 0], top[0, 1], top[1, 1]))
    nodes[2000] = (-1.0, 0.5, 0.0)
    elements[2000] = (42, (2000, number[0, 0], number[0, 1]))
    low, high = find_slabs(nodes | upper, elements | plates)
    assert segments(low.site) == segments(low.boundary) | segments(high.boundary)
    assert len(low.site) == len(low.boundary) + len(high.boundary)


def test_only_the_chosen_slab_is_built(monkeypatch):
    from lira_sapr_mcp.postprocessing import mesh

    built = []
    order = mesh._ordered
    monkeypatch.setattr(mesh, "_ordered", lambda face, xy: built.append(1) or order(face, xy))
    nodes, elements = two_levels()
    (slab,) = find_slabs(nodes, elements, within={1001, 1002})
    assert slab.number == 2 and len(built) == 2
