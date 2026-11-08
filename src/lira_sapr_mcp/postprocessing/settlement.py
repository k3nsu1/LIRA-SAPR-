"""Settlement of a slab on elastic supports: the zones over the limit, and the relative
difference of settlements |s1 - s2| / L between the vertical structures that rest on the slab,
and along a long wall. The sign is LIRA's: negative is down, and it is the size that is checked.
Limits: SP RK 5.01-102-2013, annex V, table V.1 (``STRUCTURES``) or given for the project; with
none, facts only."""

from dataclasses import dataclass

import numpy as np

from lira_sapr_mcp.postprocessing.mesh import Slab, node_edges

# SP RK 5.01-102-2013, annex V (mandatory), table V.1 "Limit deformations of foundations":
# kind -> (structures, (Δs/L)u, s max,u in m — the value in brackets of the table). Only the rows
# whose numbers read unambiguously in the text of the norm; the owner checks them against the
# official text. Notes 3 and 5 of the table may raise the settlement (+20 %; 1.5 times the mean
# for slab foundations) — the engineer applies them by giving settlement_limit_mm.
STRUCTURES: dict[str, tuple[str, float, float | None]] = {
    "rc_frame": (
        "Производственные и гражданские здания с полным железобетонным каркасом",
        0.002,
        0.10,
    ),
    "rc_frame_tied": (
        "То же, с железобетонными поясами или монолитными перекрытиями, а также здания монолитной "
        "конструкции",
        0.003,
        0.15,
    ),
    "steel_frame": (
        "Производственные и гражданские здания с полным стальным каркасом",
        0.004,
        0.15,
    ),
    "steel_frame_tied": (
        "То же, со стальным каркасом и железобетонными поясами или монолитными перекрытиями",
        0.005,
        0.18,
    ),
    "no_uneven_forces": (
        "Здания и сооружения, в конструкциях которых не возникают усилия от неравномерных осадок",
        0.006,
        None,
    ),
}
STRUCTURES_SOURCE = "СП РК 5.01-102-2013, прил. В, табл. В.1"
PLACE_AT_LEAST = 1.0  # m: pairs nearer than this to a shown one are the same place
WITHIN_A_WALL_FROM = 3.0  # m: nearer points of one wall or pylon are not compared (the owner's)


@dataclass(frozen=True)
class End:
    """One end of a place: the point (m), its settlement (m, negative down) and the slab node
    there (index). A centre of a rigid body may have no node: then the one nearest to it."""

    node: int
    x: float
    y: float
    settlement: float
    centre: bool  # the centre of a rigid body, not a node of its own


@dataclass(frozen=True)
class Difference:
    """Two points, the distance between them (m) and the relative difference."""

    a: End
    b: End
    length: float
    ratio: float


def relative_differences(
    slab: Slab,
    settlement: np.ndarray,
    limit: float = float("inf"),
    within_a_wall_from: float = WITHIN_A_WALL_FROM,
) -> list[Difference]:
    """The places to show, the worst first: only the largest while it is within the limit,
    otherwise every place over the limit. A column is one point; every pair of different
    structures is compared, and points of one wall only from ``within_a_wall_from`` apart.
    Empty when there is nothing to compare."""
    ends, owner = _points(slab, settlement)
    count = len(ends)
    if count < 2:
        return []
    xy = np.array([(e.x, e.y) for e in ends])
    s = np.array([e.settlement for e in ends])
    best, best_at = -1.0, (0, 0)
    over: list[np.ndarray] = []
    for i in range(count - 1):
        j = np.arange(i + 1, count)
        length = np.hypot(*(xy[j] - xy[i]).T)
        compared = (length > 1e-6) & ((owner[j] != owner[i]) | (length >= within_a_wall_from))
        j, length = j[compared], length[compared]
        if len(j) == 0:
            continue
        ratio = np.abs(s[j] - s[i]) / length
        k = int(np.argmax(ratio))
        if ratio[k] > best:
            best, best_at = float(ratio[k]), (i, int(j[k]))
        exceeding = ratio > limit
        if exceeding.any():
            over.append(
                np.stack(
                    [
                        np.full(exceeding.sum(), i),
                        j[exceeding],
                        ratio[exceeding],
                        length[exceeding],
                    ],
                    axis=1,
                )
            )
    if best < 0:
        return []
    if best <= limit:
        return [_difference(ends[best_at[0]], ends[best_at[1]])]
    pairs = np.concatenate(over)
    pairs = pairs[np.argsort(-pairs[:, 2], kind="stable")]
    i, j = pairs[:, 0].astype(int), pairs[:, 1].astype(int)
    length = pairs[:, 3]
    middle = (xy[i] + xy[j]) / 2
    left = np.ones(len(i), dtype=bool)
    shown = []
    while left.any():
        k = int(np.argmax(left))
        shown.append(_difference(ends[i[k]], ends[j[k]]))
        # Nearer than 0.9 of the shorter one is the same place: a bay away is another one.
        near = np.maximum(0.9 * np.minimum(length, length[k]), PLACE_AT_LEAST)
        left &= np.hypot(*(middle - middle[k]).T) > near
    return shown


def _points(slab: Slab, settlement: np.ndarray) -> tuple[list[End], np.ndarray]:
    """The points to compare and the structure of each: a column is one point, a wall all of
    its nodes. A rigid body stays plane, so the mean settlement of its nodes is the settlement
    at the mean of their positions."""
    ends: list[End] = []
    owner: list[int] = []
    for number, structure in enumerate(slab.structures):
        nodes = structure.nodes
        if structure.body:
            x, y = slab.xy[nodes].mean(axis=0)
            nearest = nodes[np.argmin(np.hypot(slab.xy[nodes, 0] - x, slab.xy[nodes, 1] - y))]
            at = [
                End(
                    int(nearest),
                    float(x),
                    float(y),
                    float(settlement[nodes].mean()),
                    len(nodes) > 1,
                )
            ]
        else:
            at = [
                End(int(i), float(slab.xy[i, 0]), float(slab.xy[i, 1]), float(settlement[i]), False)
                for i in nodes
            ]
        ends += at
        owner += [number] * len(at)
    return ends, np.array(owner, dtype=int)


def _difference(a: End, b: End) -> Difference:
    length = float(np.hypot(a.x - b.x, a.y - b.y))
    return Difference(a, b, length, abs(a.settlement - b.settlement) / length)


def zones_over(slab: Slab, settlement: np.ndarray, limit: float = float("inf")) -> list[int]:
    """Per connected zone of nodes moving more than the limit, which is a size: down (negative, as
    in LIRA) or up, its node furthest from zero (index), the furthest zone first."""
    parent = np.arange(len(settlement))
    size = np.abs(settlement)

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = int(parent[i])
        return i

    over = size > limit
    for a, b in node_edges(slab):
        if over[a] and over[b]:
            parent[root(int(a))] = root(int(b))
    furthest: dict[int, int] = {}
    for i in np.flatnonzero(over).tolist():
        r = root(i)
        if r not in furthest or size[i] > size[furthest[r]]:
            furthest[r] = i
    return sorted(furthest.values(), key=lambda i: -size[i])
