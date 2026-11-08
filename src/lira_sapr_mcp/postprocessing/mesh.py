"""Slabs in a finite element mesh: level plates, their outline and their supports.

A slab is a connected set of level plates (connected through shared nodes; each plate's
own nodes lie on one level, so a gentle fall on a fine mesh stays one slab). Its supports
are the slab nodes that something below holds up: a bar or a plate going down (column,
wall, pylon), a restraint in Z, a one-node elastic support or pile, or a rigid body or
coupled group that contains such a node. Things above the slab (columns of the next
storey) load it and are not supports.
"""

from collections import defaultdict
from dataclasses import dataclass, replace

import numpy as np

# Bending plates and shells (with their non-linear 2xx analogues); plane stress plates
# (21-30) carry no bending and are not slabs.
PLATE_TYPES = frozenset({11, 12, 15, 16, 17, 19, 41, 42, 44, 45, 46, 47})
LEVEL_TOLERANCE = 0.005  # m: nodes of one plate may differ in Z by this much
# With one-node supports at more than this share of its nodes a slab lies on soil all over: a
# mark at each of them would hide it. The owner's foundation slabs have them at 98-99 % of the
# nodes, every other slab of that model at none.
FOUNDATION_SHARE = 0.5
ALL_OVER_FROM = 1000  # supports: below this a slab is searched for spans in any case
LEAN = 0.25  # of the rise: how far a vertical structure may stray from the vertical
SPRING_BODY = "spring through a rigid body"  # a kind of support, see Slab.support_kinds


def is_plate(fe_type: int) -> bool:
    return fe_type in PLATE_TYPES or (200 < fe_type < 300 and fe_type - 200 in PLATE_TYPES)


@dataclass(frozen=True)
class Structure:
    """A vertical structure standing on a slab: its slab nodes (indices). A body is one point:
    a column on one node or on a rigid body, which tilts as a whole, so its own nodes differ
    without settling differently. Otherwise it is a wall or a pylon: plates joined at the slab."""

    nodes: np.ndarray
    body: bool


@dataclass(frozen=True)
class Slab:
    """One slab. Arrays index the slab's own nodes: ``nodes[i]`` is LIRA's number of node
    ``i``, ``xy[i]`` its plan position in meters."""

    number: int
    z: float
    elements: np.ndarray  # element numbers, ascending
    faces: list[np.ndarray]  # per element: node indices around it (3 or 4)
    nodes: np.ndarray
    xy: np.ndarray  # (n, 2)
    boundary: np.ndarray  # (m, 2, 2): outer contour and holes as segments
    boundary_nodes: np.ndarray  # bool per node
    supports: np.ndarray  # node indices, ascending
    # node index -> "column", "wall", "restraint", "spring", SPRING_BODY
    support_kinds: dict[int, str]
    carried: np.ndarray  # node indices of the vertical structures standing on the slab
    wall_lines: np.ndarray  # (m, 2) node indices: where a wall plate below meets the slab
    structures: tuple[Structure, ...] = ()  # the carried nodes, grouped
    site: np.ndarray | None = None  # outlines of all slabs of the model; None: it has only this one
    whole_elements: int = 0  # elements of the whole slab, when only some were asked for
    bedded: int = 0  # plates with bedding coefficients (C1, C2) of an elastic foundation
    points: int = 0  # what holds it, a rigid body counted as one, however many nodes it ties
    points_on_springs: int = 0  # of them: one-node elastic supports, or bodies on those alone

    @property
    def part(self) -> bool:
        """Only some plates of the slab were asked for: this is what they make up."""
        return len(self.elements) < self.whole_elements

    @property
    def springs(self) -> int:
        """Nodes on one-node elastic supports, directly or through a rigid body."""
        return sum(kind in ("spring", SPRING_BODY) for kind in self.support_kinds.values())

    @property
    def springs_through_bodies(self) -> int:
        """Those of ``springs`` held through a rigid body: the supports themselves are fewer."""
        return sum(kind == SPRING_BODY for kind in self.support_kinds.values())

    @property
    def on_foundation(self) -> bool:
        """Most of its plates have bedding coefficients, or most of what holds it are one-node
        elastic supports, under all of it or under separate points (piles): it settles, and has
        no spans (the owner's decision). A floor on columns with a spring or two still spans. A
        rigid body counts as one support: its nodes are not so many supports."""
        return self.bedded_most or self.points_on_springs * 2 > self.points

    @property
    def bedded_most(self) -> bool:
        return self.bedded > FOUNDATION_SHARE * len(self.elements)

    @property
    def springs_all_over(self) -> bool:
        """One-node supports at most of its nodes: a mark at each would hide the slab. Nodes of a
        rigid body do not count: however many, the supports under it are separate points."""
        direct = self.springs - self.springs_through_bodies
        return direct > FOUNDATION_SHARE * len(self.nodes)

    @property
    def on_soil(self) -> bool:
        """On soil under all of it, not on separate points."""
        return self.bedded_most or self.springs_all_over

    @property
    def held_all_over(self) -> bool:
        """Supports at most nodes of a large slab, not one-node springs: Z restraints or soil
        under every node. It has no spans either, and looking for them would take hours."""
        count = len(self.supports)
        return count > ALL_OVER_FROM and count > FOUNDATION_SHARE * len(self.nodes)

    def triangles(self) -> np.ndarray:
        """The faces split into triangles (quads along their first diagonal)."""
        tris = []
        for f in self.faces:
            tris.append(f[[0, 1, 2]])
            if len(f) == 4:
                tris.append(f[[0, 2, 3]])
        return np.array(tris, dtype=int).reshape(-1, 3)


def find_slabs(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, tuple[int, tuple[int, ...]]],
    restrained: set[int] = frozenset(),
    groups: list[set[int]] = (),
    within: set[int] | None = None,
    tolerance: float = LEVEL_TOLERANCE,
    bedded: set[int] = frozenset(),
) -> list[Slab]:
    """Slabs of the mesh, lowest first, numbered from 1 over the whole model.

    nodes: number -> (x, y, z) in meters. elements: number -> (FE type, nodes).
    restrained: nodes fixed in Z. groups: node sets that move together in Z (rigid
    bodies, coupled displacements). within: only the slabs with plates among these, under
    their numbers in the model; of a slab only partly among them, just those plates (``part``).
    bedded: plates with bedding coefficients.
    """
    level = []
    for el, (fe, ns) in elements.items():
        if not is_plate(fe) or len(ns) not in (3, 4) or any(n not in nodes for n in ns):
            continue
        zs = [nodes[n][2] for n in ns]
        if max(zs) - min(zs) <= tolerance:
            level.append(el)
    components = _connected(level, elements)
    slabs = []
    for comp in components:
        z = float(np.mean([nodes[n][2] for el in comp for n in elements[el][1]]))
        slabs.append((z, comp))
    slabs.sort(key=lambda s: (s[0], min(s[1])))
    holders = _holders(nodes, elements, restrained, tolerance)
    standing, feet = _standing(nodes, elements, tolerance)
    walls = _wall_pairs(nodes, elements, tolerance)
    joined = _joined(groups)
    site = None
    if len(slabs) > 1:
        site = np.concatenate([_outline(comp, nodes, elements) for _, comp in slabs])
    out = []
    for i, (z, comp) in enumerate(slabs):
        els = sorted(comp if within is None else within.intersection(comp))
        if els:
            slab = _slab(
                i + 1, z, els, nodes, elements, holders, standing, feet, walls, groups, joined
            )
            on_bed = sum(el in bedded for el in els)
            out.append(replace(slab, site=site, whole_elements=len(comp), bedded=on_bed))
    return out


def _outline(els: list[int], nodes: dict, elements: dict) -> np.ndarray:
    """(m, 2, 2): the outline of plates as ``Slab.boundary`` has it, but all plates at once:
    the key plan needs it of slabs nobody asked for, and building those takes minutes."""
    edges = []
    for size in (3, 4):
        ns = [elements[el][1] for el in els if len(elements[el][1]) == size]
        if ns:
            faces = np.array(ns, dtype=int)
            at = np.array([[nodes[n][:2] for n in row] for row in ns], dtype=float)
            off = at - at.mean(axis=1, keepdims=True)
            order = np.argsort(np.arctan2(off[..., 1], off[..., 0]), axis=1)
            faces = np.take_along_axis(faces, order, axis=1)
            edges.append(np.stack([faces, np.roll(faces, -1, axis=1)], axis=2).reshape(-1, 2))
    unique, counts = np.unique(np.sort(np.concatenate(edges), axis=1), axis=0, return_counts=True)
    return np.array(
        [[nodes[a][:2], nodes[b][:2]] for a, b in unique[counts == 1].tolist()], dtype=float
    ).reshape(-1, 2, 2)


def _connected(level: list[int], elements: dict) -> list[list[int]]:
    """Plates grouped into sets connected through shared nodes (union-find)."""
    parent = {el: el for el in level}

    def root(el: int) -> int:
        while parent[el] != el:
            parent[el] = parent[parent[el]]
            el = parent[el]
        return el

    first_at: dict[int, int] = {}
    for el in level:
        for n in elements[el][1]:
            if n in first_at:
                a, b = root(el), root(first_at[n])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                first_at[n] = el
    comps: dict[int, list[int]] = defaultdict(list)
    for el in level:
        comps[root(el)].append(el)
    return list(comps.values())


def _wall_pairs(nodes: dict, elements: dict, tolerance: float) -> list[tuple[int, int]]:
    """The two upper nodes of each plate reaching down: a piece of a wall under a slab, also
    where the wall's mesh is coarser than the slab's and its nodes are not next to each other."""
    pairs = []
    for fe, ns in elements.values():
        if not is_plate(fe) or len(ns) not in (3, 4) or any(n not in nodes for n in ns):
            continue
        low = min(nodes[n][2] for n in ns)
        high = [n for n in ns if nodes[n][2] - low > tolerance]
        if len(high) == 2:
            pairs.append((high[0], high[1]))
    return pairs


def _holders(nodes: dict, elements: dict, restrained: set[int], tolerance: float) -> dict[int, str]:
    """Nodes held from below and how: a non-slab element reaching down from the node
    (a bar - "column", a plate - "wall"), a one-node support, a restraint in Z. A node with both a
    bar and a spring is held by the bar, whichever element comes first in the model: the numbering
    must not change how a slab is checked."""
    held: dict[int, str] = {n: "restraint" for n in restrained}
    for fe, ns in elements.values():
        known = [n for n in ns if n in nodes]
        if len(known) < 2:
            continue
        low = min(nodes[n][2] for n in known)
        kind = "wall" if is_plate(fe) or len(known) > 2 else "column"
        for n in known:
            if nodes[n][2] - low > tolerance:
                held.setdefault(n, kind)
    for _, ns in elements.values():
        if len(ns) == 1:  # elastic support, link at a node, pile (51, 56, 57, 2xx)
            held.setdefault(ns[0], "spring")
    return held


def _standing(nodes: dict, elements: dict, tolerance: float) -> tuple[set[int], list[list[int]]]:
    """Nodes with a vertical structure reaching up from them: a bar leaning less than
    ``LEAN`` of its rise (a strut or a stair is not one), a plate standing in a vertical plane.
    And the feet of those plates: the lower nodes of each, which belong to one wall."""
    out: set[int] = set()
    feet: list[list[int]] = []
    for _, ns in elements.values():
        known = [n for n in ns if n in nodes]
        if not 2 <= len(known) <= 4:
            continue
        high = max(nodes[n][2] for n in known)
        lower = [n for n in known if high - nodes[n][2] > tolerance]
        if lower and _upright([nodes[n] for n in known]):
            out.update(lower)
            if len(known) > 2 and len(lower) > 1:
                feet.append(lower)
    return out, feet


def _upright(points: list[tuple[float, float, float]]) -> bool:
    x0, y0, _ = points[0]
    rise = max(p[2] for p in points) - min(p[2] for p in points)
    far = max(points, key=lambda p: (p[0] - x0) ** 2 + (p[1] - y0) ** 2)
    dx, dy = far[0] - x0, far[1] - y0
    length = (dx * dx + dy * dy) ** 0.5
    if len(points) == 2:
        return length <= LEAN * rise
    if length <= LEAN * rise:
        return True
    return all(abs((p[0] - x0) * dy - (p[1] - y0) * dx) / length <= LEAN * rise for p in points)


def _slab(
    number: int,
    z: float,
    els: list[int],
    nodes: dict,
    elements: dict,
    holders: dict[int, str],
    standing: set[int],
    feet: list[list[int]],
    walls: list[tuple[int, int]],
    groups: list[set[int]],
    joined: list[set[int]],
) -> Slab:
    own = sorted({n for el in els for n in elements[el][1]})
    index = {n: i for i, n in enumerate(own)}
    xy = np.array([nodes[n][:2] for n in own], dtype=float)
    faces = [_ordered(np.array([index[n] for n in elements[el][1]]), xy) for el in els]
    edges: dict[tuple[int, int], int] = defaultdict(int)
    for f in faces:
        for a, b in zip(f, np.roll(f, -1), strict=True):
            edges[(min(a, b), max(a, b))] += 1
    outline = [e for e, count in edges.items() if count == 1]
    on_boundary = np.zeros(len(own), dtype=bool)
    for a, b in outline:
        on_boundary[[a, b]] = True
    kinds = {index[n]: holders[n] for n in own if n in holders}
    carried = {index[n] for n in own if n in standing}
    held = _held_bodies(joined, holders, index)
    for group, on_springs in held:  # what holds a rigid body holds all its nodes
        for n in group:
            if n in index:
                kinds.setdefault(index[n], SPRING_BODY if on_springs else "column")
    in_bodies = {index[n] for group, _ in held for n in group if n in index}
    alone = [kind for i, kind in kinds.items() if i not in in_bodies]
    bodies = []
    for group in groups:
        if any(n in standing for n in group):
            bodies.append([index[n] for n in group if n in index])
            carried.update(bodies[-1])
    return Slab(
        number=number,
        z=z,
        elements=np.array(els, dtype=int),
        faces=faces,
        nodes=np.array(own, dtype=int),
        xy=xy,
        boundary=np.array([[xy[a], xy[b]] for a, b in outline], dtype=float).reshape(-1, 2, 2),
        boundary_nodes=on_boundary,
        supports=np.array(sorted(kinds), dtype=int),
        support_kinds=kinds,
        points=len(held) + len(alone),
        points_on_springs=sum(on_springs for _, on_springs in held) + alone.count("spring"),
        carried=np.array(sorted(carried), dtype=int),
        wall_lines=np.array(
            [(index[a], index[b]) for a, b in walls if a in index and b in index], dtype=int
        ).reshape(-1, 2),
        structures=_structures(
            carried, bodies, [[index[n] for n in foot if n in index] for foot in feet]
        ),
    )


def _held_bodies(
    groups: list[set[int]], holders: dict[int, str], index: dict[int, int]
) -> list[tuple[set[int], bool]]:
    """The rigid bodies that reach the slab and are held from below, and whether by springs alone.
    ``groups``: from ``_joined``."""
    held = []
    for group in groups:
        holding = {holders[n] for n in group if n in holders}
        if holding and any(n in index for n in group):
            held.append((group, holding == {"spring"}))
    return held


def _joined(groups: list[set[int]]) -> list[set[int]]:
    """Groups that share a node move as one, whatever the model lists them as or in what order: a
    body repeated as a coupled group, or inside another, is not another support."""
    merged: list[set[int]] = []
    owner: dict[int, int] = {}
    for group in groups:
        together = set(group)
        for k in {owner[n] for n in group if n in owner}:
            together |= merged[k]
            merged[k] = set()
        merged.append(together)
        owner.update(dict.fromkeys(together, len(merged) - 1))
    return [group for group in merged if group]


def _structures(
    carried: set[int], bodies: list[list[int]], feet: list[list[int]]
) -> tuple[Structure, ...]:
    """The carried nodes joined by the rigid bodies and by the plates standing on them. What a
    plate joins is a wall or a pylon, whatever rigid body it also takes in."""
    parent = {i: i for i in carried}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for together in (*bodies, *feet):
        for i in together[1:]:
            parent[root(i)] = root(together[0])
    walled = {root(foot[0]) for foot in feet if len(foot) > 1}
    found: dict[int, list[int]] = defaultdict(list)
    for i in sorted(carried):
        found[root(i)].append(i)
    return tuple(
        Structure(np.array(members, dtype=int), r not in walled) for r, members in found.items()
    )


def _ordered(face: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Face nodes in order around the face (by angle about the centre), so that a quad
    whose nodes LIRA lists crosswise still makes a proper polygon."""
    pts = xy[face]
    c = pts.mean(axis=0)
    order = np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))
    return face[order]


def outline_loops(slab: Slab, chosen: np.ndarray | None = None) -> list[np.ndarray]:
    """Closed outlines (plan points, m) of the union of the chosen elements (a bool per
    face; all by default): its outer contours and holes, from the face edges used
    once."""
    if chosen is None:
        chosen = np.ones(len(slab.faces), dtype=bool)
    count: dict[tuple[int, int], int] = defaultdict(int)
    for f, keep in zip(slab.faces, chosen, strict=True):
        if keep:
            for a, b in zip(f, np.roll(f, -1), strict=True):
                count[(min(a, b), max(a, b))] += 1
    edges = [e for e, n in count.items() if n == 1]
    around: dict[int, list[int]] = defaultdict(list)
    for a, b in edges:
        around[a].append(b)
        around[b].append(a)
    used: set[tuple[int, int]] = set()
    loops = []
    for a, b in edges:
        if (a, b) in used:
            continue
        loop = [a]
        prev, cur = a, b
        used.add((a, b))
        while cur != a:
            loop.append(cur)
            nxt = next(
                (n for n in around[cur] if (min(cur, n), max(cur, n)) not in used and n != prev),
                None,
            )
            if nxt is None:
                break
            used.add((min(cur, nxt), max(cur, nxt)))
            prev, cur = cur, nxt
        if len(loop) >= 3:
            loops.append(slab.xy[loop])
    return loops


def node_edges(slab: Slab) -> np.ndarray:
    """(m, 2): the element edges of a slab as pairs of node indices, each once."""
    return _edges_and_counts(slab)[0]


def outline_edges(slab: Slab) -> np.ndarray:
    """(m, 2): the edges of a slab's outline and openings as pairs of node indices."""
    edges, counts = _edges_and_counts(slab)
    return edges[counts == 1]


def _edges_and_counts(slab: Slab) -> tuple[np.ndarray, np.ndarray]:
    pairs = np.concatenate([np.stack([f, np.roll(f, -1)], axis=1) for f in slab.faces])
    return np.unique(np.sort(pairs, axis=1), axis=0, return_counts=True)


def polygon_area(pts: np.ndarray) -> float:
    """Plan area of a polygon (its points in order)."""
    x, y = pts[:, 0], pts[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2
