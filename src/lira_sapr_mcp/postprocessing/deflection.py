"""Deflection of a slab relative to its supports, and its limit set for the project.

A deflection is read along a line between two points of supports, as along a line of
sight, and only at the points of that line: the displacement of the slab at the point less
that of the straight line between its two ends, with LIRA's sign (minus is down, so a slab
below the line deflects by a negative value). The span is the length of the line. A line
counts when it stays inside the slab (slab edges and openings stop it), passes through no
other support and crosses no line of supports (a wall): those points
divide it into spans of their own.

A free edge that no such line comes near overhangs. The overhang is measured the shortest
way from the edge to a support, and the deflection is read at the line's end on the edge,
relative to the support; the span is twice the overhang.

A point of a line usually lies inside an element: its displacement is interpolated in the
element from the element's nodes.

The field drawn over a slab is one rule for every node: its displacement less that of the surface
through the supports. Between supports the surface is the plane through three of them, beyond the
outer ones the straight line between the two nearest. With every support at zero the field is
exactly LIRA's displacement. The checks stay with the lines.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from matplotlib.tri import Triangulation

from lira_sapr_mcp.postprocessing.mesh import Slab, node_edges, outline_edges, outline_loops

# Kazakhstan: SP RK EN 1993-1-1 7.2.1 and national annex NP.2.21 leave the limit of deflections
# to each project. ``Limit``: span / N as the number N (e.g. 250), or a function of the spans (m)
# giving the allowed deflections (m) for a table the project adopts; None: no limit, the readings
# are ranked by f / span and nothing is called over it.
Limit = float | Callable[[np.ndarray], np.ndarray] | None

SUPPORT_CLEARANCE = 0.1  # m: a line this near another support passes through it
BLOCKERS_NEAR = 1.0  # m: supports nearer than this are checked against every line
PAIRS_AT_ONCE = 2_000_000  # lines x edges per numpy step
POINTS_AT_ONCE = 500_000  # points of lines per numpy step
INSIDE = 1e-6  # of a triangle's size: a point this far out on rounding is still in it
PANEL_RISE = 0.001  # m: a hollow this shallow is not a panel of its own
RIM_POINTS_AT_ONCE = 2_000  # nodes against the rim of the supports per numpy step


@dataclass(frozen=True)
class Lines:
    """One reading per line, at its point with the largest deflection: the slab node
    indices of its ends (``b`` = -1: a cantilever, from support ``a`` to a free edge), its
    far end (x, y), the point (x, y), the displacements of the slab and of the straight line
    there (m), the deflection f (m, as in LIRA: negative = below the line), span, allowed
    deflection (m), |f| / allowed."""

    a: np.ndarray
    b: np.ndarray
    end: np.ndarray  # (k, 2)
    point: np.ndarray  # (k, 2)
    w_point: np.ndarray
    w_line: np.ndarray
    f: np.ndarray
    span: np.ndarray
    allowed: np.ndarray
    ratio: np.ndarray

    def __len__(self) -> int:
        return len(self.a)

    def angle(self, slab: Slab, i: int) -> float:
        """The direction of line ``i``, degrees from X, 0-180."""
        dx, dy = self.end[i] - slab.xy[self.a[i]]
        return round(float(np.degrees(np.arctan2(dy, dx)) % 180), 1)


@dataclass(frozen=True)
class Deflections:
    """The lines of a slab, and per slab node (NaN at the supports): the deflection f (m) from
    the surface through the supports; the span (m) and allowed deflection (m) of the line passing
    nearest with the largest ratio, or for a node no line passes, as for an overhang from its
    nearest support; |f| / allowed."""

    lines: Lines
    f: np.ndarray
    span: np.ndarray
    allowed: np.ndarray
    ratio: np.ndarray

    def worst(self) -> int | None:
        """The line with the largest ratio, or None when there is no line."""
        return int(np.argmax(self.lines.ratio)) if len(self.lines) else None


def allowed_deflections(spans: np.ndarray, fraction: Limit = None) -> np.ndarray:
    """Allowed deflection (m) of each span (m): span / N, or the project's function of the span.
    With no limit (None) the span itself, so that |f| / allowed is f / span and ranks the
    readings without a verdict."""
    spans = np.asarray(spans, dtype=float)
    if fraction is None:
        return spans
    if callable(fraction):
        return np.asarray(fraction(spans), dtype=float)
    return spans / fraction


def worst_spans(d: Deflections, count: int = 3) -> list[int]:
    """Lines of the most dangerous places of a slab, the worst first: at least ``count``,
    and every place over the limit. After each pick the readings nearer than 0.9 of the
    shorter of the two spans are left out, so that the next one is another place, not beside
    it in the same one; a short span next to a long one is a place of its own."""
    lines = d.lines
    ratio = lines.ratio.copy()
    picked: list[int] = []
    while len(ratio) and np.isfinite(ratio.max()) and (len(picked) < count or ratio.max() > 1):
        i = int(np.argmax(ratio))
        picked.append(i)
        near = 0.9 * np.minimum(lines.span, lines.span[i])
        ratio[np.hypot(*(lines.point - lines.point[i]).T) <= near] = -np.inf
    return picked


def panel_lows(slab: Slab, w: np.ndarray, rise: float = PANEL_RISE) -> list[int]:
    """Slab node indices of the lowest point of each panel, the lowest first. A panel is a
    hollow of the displaced slab at least ``rise`` deep, from the point of its bottom to the
    level where it runs into a lower hollow: nothing of the mesh or the supports is used,
    so a panel is what the slab did. The nodes of supports do not count."""
    around: list[list[int]] = [[] for _ in w]
    for a, b in node_edges(slab):
        around[a].append(int(b))
        around[b].append(int(a))
    parent = np.full(len(w), -1)

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = int(parent[i])
        return i

    bottom: dict[int, int] = {}
    lows = []
    for i in np.argsort(w, kind="stable").tolist():
        parent[i] = i
        bottom[i] = i
        for j in around[i]:
            if parent[j] < 0:
                continue
            a, b = root(i), root(j)
            if a == b:
                continue
            if w[bottom[a]] > w[bottom[b]]:
                a, b = b, a  # a keeps the lower bottom; b's hollow ends here
            if w[i] - w[bottom[b]] >= rise:
                lows.append(bottom[b])
            parent[b] = a
    top = w.max()
    lows.extend(bottom[r] for r in {root(i) for i in range(len(w))} if top - w[bottom[r]] >= rise)
    supported = set(slab.supports.tolist())
    return sorted((i for i in set(lows) if i not in supported), key=lambda i: (w[i], i))


def relative_deflections(slab: Slab, w: np.ndarray, fraction: Limit = None) -> Deflections:
    """Deflections of a slab relative to its supports along lines between them (w: Z
    displacement of each slab node in meters, positive up). A slab on an elastic foundation
    has no spans between supports: nothing is measured."""
    n = len(slab.nodes)
    field = {k: np.full(n, np.nan) for k in ("f", "span", "allowed", "ratio")}
    if len(slab.supports) == 0 or slab.on_foundation or slab.held_all_over:
        return Deflections(_no_lines(), **field)
    surface = _slab_surface(slab, w)
    edges = _straight_edges(slab)
    corners = np.unique(edges.reshape(-1, 2), axis=0)
    pairs = _span_pairs(slab, edges, corners)
    spans = _read_lines(
        surface, slab.supports[pairs[:, 0]], slab.supports[pairs[:, 1]], fraction, field
    )
    levers = _overhangs(slab, edges, corners, field)
    overhangs = _read_lines(surface, levers[:, 0], levers[:, 1], fraction, field, cantilever=True)
    reference, beyond = _through_supports(slab, w)
    no_line = np.isnan(field["span"])
    no_line[slab.supports] = False
    field["f"] = w - reference
    _limits_without_a_line(slab, field, no_line, beyond, fraction)
    field["ratio"] = np.abs(field["f"]) / field["allowed"]
    for values in field.values():
        values[slab.supports] = np.nan
    over = _over_without_a_line(slab, w, reference, field, no_line & beyond)
    lines = Lines(
        *(
            np.concatenate([getattr(part, name) for part in (spans, overhangs, over)])
            for name in Lines.__dataclass_fields__
        )
    )
    return Deflections(lines, **field)


def _nearest_supports(slab: Slab, nodes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per node, the slab index of its nearest support and the distance to it."""
    held = slab.xy[slab.supports]
    which = np.zeros(len(nodes), dtype=int)
    gap = np.zeros(len(nodes), dtype=float)
    for k, i in enumerate(nodes.tolist()):  # one at a time: all at once takes nodes x supports
        gaps = np.hypot(*(held - slab.xy[i]).T)
        which[k] = int(np.argmin(gaps))
        gap[k] = gaps[which[k]]
    return slab.supports[which], gap


def _limits_without_a_line(
    slab: Slab,
    field: dict[str, np.ndarray],
    no_line: np.ndarray,
    beyond: np.ndarray,
    fraction: Limit,
) -> None:
    """The limit of a node no line passes: beyond the outer supports that of an overhang from its
    nearest support, within them that of the nearest node on a line, in the same panel."""
    read = np.flatnonzero(np.isfinite(field["span"]))
    read = read[~np.isin(read, slab.supports)]
    out = no_line & beyond if len(read) else no_line
    nodes = np.flatnonzero(out)
    span = 2 * _nearest_supports(slab, nodes)[1]
    field["span"][nodes] = span
    # A node right over a support (a doubled node) has no overhang to limit.
    field["allowed"][nodes] = np.where(span > 1e-6, allowed_deflections(span, fraction), np.inf)
    for node in np.flatnonzero(no_line & ~out).tolist():
        nearest = read[np.argmin(np.hypot(*(slab.xy[read] - slab.xy[node]).T))]
        field["span"][node], field["allowed"][node] = (
            field["span"][nearest],
            field["allowed"][nearest],
        )


def _over_without_a_line(
    slab: Slab,
    w: np.ndarray,
    reference: np.ndarray,
    field: dict[str, np.ndarray],
    overhanging: np.ndarray,
) -> Lines:
    """A node beyond the outer supports that no line passes, over its limit, is a place to report
    like the others: an overhang from its nearest support, read off the surface through the
    supports. Within the supports such a node is only shown, as the owner decided for a point off
    the lines."""
    nodes = np.flatnonzero(overhanging & (field["ratio"] > 1))
    return Lines(
        a=_nearest_supports(slab, nodes)[0],
        b=np.full(len(nodes), -1),
        end=slab.xy[nodes],
        point=slab.xy[nodes],
        w_point=w[nodes],
        w_line=reference[nodes],
        f=field["f"][nodes],
        span=field["span"][nodes],
        allowed=field["allowed"][nodes],
        ratio=field["ratio"][nodes],
    )


def _through_supports(slab: Slab, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The displacement of the surface through the supports at every slab node: within their
    triangles (Delaunay) the plane through three of them, beyond the outer ones the straight line
    between the two nearest. Supports in one line have only that line, a single one its level.
    And which nodes lie beyond the outer supports."""
    points, first = np.unique(slab.xy[slab.supports], axis=0, return_index=True)
    values = w[slab.supports[first]]
    if len(points) == 1:
        return np.full(len(w), float(values[0])), np.ones(len(w), dtype=bool)
    try:
        triangles = Triangulation(points[:, 0], points[:, 1]).triangles
    except (RuntimeError, ValueError):  # all in one line
        triangles = np.zeros((0, 3), dtype=int)
    reference = np.full(len(w), np.nan)
    if len(triangles):
        # Triangles between supports are far larger than elements: a cell per 200th of the plan.
        cell = float(np.hypot(*np.ptp(points, axis=0))) / 200
        surface = _Surface(points, triangles, values, 3 * cell)
        reference = surface.at(slab.xy)[0]
        triangles = surface.triangles
    beyond = np.isnan(reference)
    if beyond.any():
        reference[beyond] = _on_the_rim(slab.xy[beyond], points, _rim(points, triangles), values)
    return reference, beyond


def _rim(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """(m, 2) point indices: the sides of the triangles used once, or with no triangles (the
    points in one line) each point joined to the next along that line."""
    if len(triangles) == 0:
        direction = points[np.argmax(np.hypot(*(points - points[0]).T))] - points[0]
        order = np.argsort((points - points[0]) @ direction, kind="stable")
        return np.stack([order[:-1], order[1:]], axis=1)
    sides = np.sort(triangles[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2), axis=1)
    unique, counts = np.unique(sides, axis=0, return_counts=True)
    return unique[counts == 1]


def _on_the_rim(
    where: np.ndarray, points: np.ndarray, rim: np.ndarray, values: np.ndarray
) -> np.ndarray:
    """The value at the nearest point of the rim: on the straight line between two supports."""
    a, along = points[rim[:, 0]], points[rim[:, 1]] - points[rim[:, 0]]
    out = np.empty(len(where))
    for start in range(0, len(where), RIM_POINTS_AT_ONCE):
        part = where[start : start + RIM_POINTS_AT_ONCE]
        rel = part[:, None] - a[None]
        t = np.clip((rel * along).sum(-1) / (along * along).sum(-1), 0, 1)
        side = np.argmin(np.hypot(*np.moveaxis(rel - t[..., None] * along, -1, 0)), axis=1)
        share = t[np.arange(len(part)), side]
        out[start : start + RIM_POINTS_AT_ONCE] = (1 - share) * values[rim[side, 0]] + (
            share * values[rim[side, 1]]
        )
    return out


def _slab_surface(slab: Slab, w: np.ndarray) -> "_Surface":
    sides = np.linalg.norm(np.diff(slab.xy[node_edges(slab)], axis=1), axis=2)
    return _Surface(slab.xy, slab.triangles(), w, max(float(np.median(sides)), 1e-3))


def _no_lines() -> Lines:
    empty = np.zeros(0)
    none = np.zeros(0, dtype=int)
    plan = np.zeros((0, 2))
    return Lines(none, none, plan, plan, empty, empty, empty, empty, empty, empty)


class _Surface:
    """Values over triangles in plan (the displaced slab, the surface through its supports):
    interpolates w at plan points within the triangles, found through a grid of cells over the
    plan. A triangle without area (a collapsed plate) reads nothing; where plates overlap, one of
    them reads. ``size``: a typical side."""

    def __init__(self, xy: np.ndarray, triangles: np.ndarray, w: np.ndarray, size: float) -> None:
        self.xy, self.w, self.size = xy, w, size
        a, b, c = np.moveaxis(xy[triangles], 1, 0)
        area = _cross(b - a, c - a)
        solid = np.abs(area) > 1e-12
        self.triangles, self.area = triangles[solid], area[solid]
        self.cell = self.size / 3  # fewer triangles to try per point than cells of a side
        self.origin = xy.min(axis=0) - self.cell
        self.columns = int(np.ptp(xy[:, 0]) / self.cell) + 4
        corners = xy[self.triangles]
        margin = 1e-6
        low, high = (
            self._cells(corners.min(axis=1) - margin),
            self._cells(corners.max(axis=1) + margin),
        )
        wide, tall = (high - low + 1).T
        which = np.repeat(np.arange(len(self.triangles)), wide * tall)
        k = np.arange(len(which)) - np.repeat(np.cumsum(wide * tall) - wide * tall, wide * tall)
        cell = low[which, 0] + k % wide[which] + self.columns * (low[which, 1] + k // wide[which])
        order = np.argsort(cell, kind="stable")
        self.cells, self.in_cells = cell[order], which[order]

    def _cells(self, points: np.ndarray) -> np.ndarray:
        return np.floor((points - self.origin) / self.cell).astype(int)

    def at(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """w at each point (NaN outside the mesh) and the slab node nearest to it in its
        triangle (-1 outside)."""
        x, y = self._cells(points).T
        wanted = x + self.columns * y
        start = np.searchsorted(self.cells, wanted)
        counts = np.searchsorted(self.cells, wanted, side="right") - start
        row = np.repeat(np.arange(len(points)), counts)
        place = (
            np.repeat(start, counts)
            + np.arange(len(row))
            - np.repeat(np.cumsum(counts) - counts, counts)
        )
        which = self.in_cells[place]
        corners = self.triangles[which]
        a, b, c = self.xy[corners[:, 0]], self.xy[corners[:, 1]], self.xy[corners[:, 2]]
        p, area = points[row], self.area[which]
        weights = np.stack(
            [_cross(b - p, c - p) / area, _cross(c - p, a - p) / area, _cross(a - p, b - p) / area],
            axis=1,
        )
        inside = np.flatnonzero((weights >= -INSIDE).all(axis=1))
        first = np.full(len(points), -1)
        first[row[inside[::-1]]] = inside[::-1]  # the first triangle that has the point
        found = np.flatnonzero(first >= 0)
        hit = first[found]
        values, nearest = np.full(len(points), np.nan), np.full(len(points), -1)
        values[found] = (weights[hit] * self.w[corners[hit]]).sum(axis=1)
        nearest[found] = corners[hit, np.argmax(weights[hit], axis=1)]
        return values, nearest


def _span_pairs(slab: Slab, edges: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """(k, 2): pairs of indices into the supports, joined by a line that counts."""
    pts = slab.xy[slab.supports]
    count = len(pts)
    where = np.full(len(slab.nodes), -1)
    where[slab.supports] = np.arange(count)
    ends = where[node_edges(slab)]
    # Lines of supports: mesh edges between two of them, and where a wall below meets the slab
    # (the same, unless the wall's mesh is coarser and its nodes are not next to each other).
    ends = np.concatenate([ends, where[slab.wall_lines]])
    walls = ends[(ends >= 0).all(axis=1)]
    next_to = np.zeros((count, count), dtype=bool)
    next_to[walls[:, 0], walls[:, 1]] = next_to[walls[:, 1], walls[:, 0]] = True
    wall_a, wall_b = pts[walls[:, 0]], pts[walls[:, 1]]
    first, second = [], []
    for i in range(count - 1):
        rel = pts - pts[i]
        distance = np.hypot(rel[:, 0], rel[:, 1])
        targets = i + 1 + np.flatnonzero((distance[i + 1 :] > 1e-6) & ~next_to[i, i + 1 :])
        clear = targets[~_through_a_support(rel, distance, targets)]
        first.append(np.full(len(clear), i))
        second.append(clear)
    i, j = np.concatenate(first or [np.zeros(0, int)]), np.concatenate(second or [np.zeros(0, int)])
    found = [np.zeros((0, 2), dtype=int)]
    block = max(1, PAIRS_AT_ONCE // max(len(walls), len(edges), len(corners), 1))
    for start in range(0, len(i), block):
        ii, jj = i[start : start + block], j[start : start + block]
        a, b = pts[ii], pts[jj]
        clear = ~_crossing(a, b, wall_a, wall_b)
        ii, jj, a, b = ii[clear], jj[clear], a[clear], b[clear]
        on_outline = slab.boundary_nodes[slab.supports[ii]]
        seen = ~_hidden(a, b, edges, corners, on_outline)
        found.append(np.stack([ii[seen], jj[seen]], axis=1))
    return np.concatenate(found)


def _through_a_support(rel: np.ndarray, distance: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Whether another support lies on the line from one support to each target: near the line
    (under SUPPORT_CLEARANCE) and not at its ends. ``rel``, ``distance``: every support as seen
    from the first one. A support further than BLOCKERS_NEAR can only lie on a line within
    a narrow angle of its own direction, so only such pairs are looked at."""
    angle = np.arctan2(rel[:, 1], rel[:, 0])
    others = np.flatnonzero(distance > SUPPORT_CLEARANCE)
    near = others[distance[others] < BLOCKERS_NEAR]
    far = others[distance[others] >= BLOCKERS_NEAR]
    far = far[np.argsort(angle[far], kind="stable")]
    around = np.concatenate([angle[far] - 2 * np.pi, angle[far], angle[far] + 2 * np.pi])
    wide = np.arcsin(SUPPORT_CLEARANCE / BLOCKERS_NEAR)
    low = np.searchsorted(around, angle[targets] - wide)
    counts = np.searchsorted(around, angle[targets] + wide, side="right") - low
    row = np.repeat(np.arange(len(targets)), counts)
    place = np.arange(len(row)) - np.repeat(np.cumsum(counts) - counts, counts)
    within = np.tile(far, 3)[np.repeat(low, counts) + place]
    # the pairs to look at: every near support with every line, and the far ones within the angle
    row = np.concatenate([np.repeat(np.arange(len(targets)), len(near)), row])
    other = np.concatenate([np.tile(near, len(targets)), within])
    line = rel[targets[row]]
    length = distance[targets[row]]
    along = (rel[other] * line).sum(axis=1) / length
    off = np.abs(_cross(line, rel[other])) / length
    on_it = (
        (off < SUPPORT_CLEARANCE)
        & (along > SUPPORT_CLEARANCE)
        & (along < length - SUPPORT_CLEARANCE)
    )
    blocked = np.zeros(len(targets), dtype=bool)
    blocked[row[on_it]] = True
    return blocked


def _crossing(a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Whether each segment a-b properly crosses one of the segments c-d."""
    out = np.zeros(len(a), dtype=bool)
    if len(c) == 0 or len(a) == 0:
        return out
    tiny = 1e-12
    r = b - a
    s1 = _cross(r[:, None], c[None] - a[:, None])
    s2 = _cross(r[:, None], d[None] - a[:, None])
    k, e = np.nonzero(s1 * s2 < -tiny)
    side = d[e] - c[e]
    hit = _cross(side, a[k] - c[e]) * _cross(side, b[k] - c[e]) < -tiny
    out[k[hit]] = True
    return out


def _read_lines(
    surface: _Surface,
    a: np.ndarray,
    b: np.ndarray,
    fraction: Limit,
    field: dict[str, np.ndarray],
    cantilever: bool = False,
) -> Lines:
    """Readings of lines from slab node a to slab node b at their own points; updates the
    field of the slab nodes. A cantilever: from support a to the free edge at node b."""
    if len(a) == 0:
        return _no_lines()
    length = np.hypot(*(surface.xy[b] - surface.xy[a]).T)
    # About two points per element of the usual size; a much finer zone gets stepped over: the
    # readings there are near a support and small, and its nodes may stay without a value.
    step = surface.size / 2
    intervals = np.maximum(np.ceil(length / step).astype(int), 2)
    total = np.cumsum(intervals)
    readings = []
    start = 0
    while start < len(a):
        done = total[start - 1] if start else 0
        stop = max(start + 1, int(np.searchsorted(total, done + POINTS_AT_ONCE)))
        part = slice(start, stop)
        readings.append(
            _read_block(surface, a[part], b[part], length[part], intervals[part], fraction,
                        field, cantilever)
        )  # fmt: skip
        start = stop
    return Lines(
        *(
            np.concatenate([getattr(r, name) for r in readings])
            for name in Lines.__dataclass_fields__
        )
    )


def _read_block(
    surface: _Surface,
    a: np.ndarray,
    b: np.ndarray,
    length: np.ndarray,
    intervals: np.ndarray,
    fraction: Limit,
    field: dict[str, np.ndarray],
    cantilever: bool,
) -> Lines:
    xy, w = surface.xy, surface.w
    # The inner points of each line in turn; a cantilever's includes its free end.
    extra = 1 if cantilever else 0
    counts = intervals - 1 + extra
    line = np.repeat(np.arange(len(a)), counts)
    first = np.r_[0, np.cumsum(counts)[:-1]]
    t = (np.arange(len(line)) - first[line] + 1) / intervals[line]
    p = xy[a][line] + t[:, None] * (xy[b] - xy[a])[line]
    w_point, nearest = surface.at(p)
    # --8<-- [start:readings]
    if cantilever:  # relative to the support; the span is twice the overhang
        w_line = w[a][line]
        span = 2 * length[line]
    else:  # the straight line between the two ends of the line, at the same point
        w_line = (1 - t) * w[a][line] + t * w[b][line]
        span = length[line]
    f = w_point - w_line
    allowed = allowed_deflections(span, fraction)
    ratio = np.abs(f) / allowed
    # --8<-- [end:readings]
    known = np.flatnonzero(np.isfinite(ratio))
    _update_field(field, nearest[known], f[known], span[known], allowed[known], ratio[known])
    if cantilever:  # at the end of the overhang
        last = np.cumsum(counts) - 1
        head = last[np.isfinite(ratio[last])]
    else:  # per line, the point with the largest ratio
        order = known[np.lexsort((-ratio[known], line[known]))]
        head = order[np.r_[True, line[order][1:] != line[order][:-1]]] if len(order) else order
    which = line[head]
    return Lines(
        a=a[which],
        b=np.full(len(which), -1) if cantilever else b[which],
        end=xy[b[which]],
        point=p[head],
        w_point=w_point[head],
        w_line=w_line[head],
        f=f[head],
        span=span[head],
        allowed=allowed[head],
        ratio=ratio[head],
    )


def _update_field(field: dict[str, np.ndarray], nodes, f, span, allowed, ratio) -> None:
    """Each node keeps the reading with the largest ratio of the points nearest to it."""
    known = np.where(np.isnan(field["ratio"]), -np.inf, field["ratio"])
    better = np.flatnonzero(ratio > known[nodes])
    order = better[np.argsort(ratio[better], kind="stable")]  # the largest is written last
    at = nodes[order]
    field["f"][at] = f[order]
    field["span"][at] = span[order]
    field["allowed"][at] = allowed[order]
    field["ratio"][at] = ratio[order]


def _overhangs(
    slab: Slab, edges: np.ndarray, corners: np.ndarray, field: dict[str, np.ndarray]
) -> np.ndarray:
    """(k, 2) slab node indices: the support and the free edge node of each cantilever line,
    the shortest way from a free edge to a support: from the outline nodes nearer to a support
    than the nodes beside them on the outline (all of them along a wall parallel to the edge).
    A node beside a support on the outline is no overhang, nor one a line between supports
    comes near."""
    supported = np.zeros(len(slab.nodes), dtype=bool)
    supported[slab.supports] = True
    free = np.flatnonzero(slab.boundary_nodes & ~supported)
    targets = slab.xy[slab.supports]
    nearest = {}  # free node -> (slab index of its nearest visible support, distance)
    for node in free.tolist():
        origin = slab.xy[node]
        distance = np.hypot(*(targets - origin).T)
        order = np.argsort(distance, kind="stable")
        for start in range(0, len(order), 16):  # most see one of their nearest
            part = order[start : start + 16]
            part = part[distance[part] > 1e-9]
            if len(part) == 0:
                continue
            hidden = _hidden(
                np.tile(origin, (len(part), 1)), targets[part], edges, corners,
                np.ones(len(part), dtype=bool),
            )  # fmt: skip
            if not hidden.all():
                seen = part[np.argmin(hidden)]
                nearest[node] = (int(slab.supports[seen]), float(distance[seen]))
                break
    beside: dict[int, list[int]] = {}
    for a, b in outline_edges(slab).tolist():
        beside.setdefault(a, []).append(b)
        beside.setdefault(b, []).append(a)
    found = []
    for node, (support, distance) in nearest.items():
        if not np.isnan(field["ratio"][node]) or any(supported[n] for n in beside[node]):
            continue
        if all(n not in nearest or nearest[n][1] >= distance - 1e-6 for n in beside[node]):
            found.append((support, node))
    return np.array(found, dtype=int).reshape(-1, 2)


def _straight_edges(slab: Slab) -> np.ndarray:
    """The slab outline (contours and openings) as segments, with the points in the
    middle of straight runs left out: a straight side is one segment, not one per
    element."""
    out = []
    for loop in outline_loops(slab):
        u = loop - np.roll(loop, 1, axis=0)
        v = np.roll(loop, -1, axis=0) - loop
        scale = np.linalg.norm(u, axis=1) * np.linalg.norm(v, axis=1)
        turn = np.abs(_cross(u, v)) > 1e-9 * np.maximum(scale, 1e-12)
        pts = loop[turn] if turn.sum() >= 3 else loop
        out.extend(zip(pts, np.roll(pts, -1, axis=0), strict=True))
    return np.array(out, dtype=float).reshape(-1, 2, 2)


def _cross(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]


def _hidden(
    origins: np.ndarray,
    targets: np.ndarray,
    edges: np.ndarray,
    corners: np.ndarray,
    on_outline: np.ndarray,
) -> np.ndarray:
    """Whether the segment from each origin to its target leaves the slab - crosses an
    edge, or runs into an opening or across a notch past a corner. Touching the outline
    (ending on it, running along it, grazing a corner) does not count."""
    r = targets - origins  # (k, 2)
    hidden = np.zeros(len(r), dtype=bool)
    if len(edges) == 0:
        return hidden
    a, b = edges[:, 0], edges[:, 1]  # (e, 2)
    # proper crossings: the ends of each on strictly opposite sides of the other
    tiny = 1e-12
    s1 = _cross(r[:, None], a[None] - origins[:, None])  # (k, e)
    s2 = _cross(r[:, None], b[None] - origins[:, None])
    s3 = _cross((b - a)[None], origins[:, None] - a[None])
    s4 = _cross((b - a)[None], targets[:, None] - a[None])
    hidden |= ((s1 * s2 < -tiny) & (s3 * s4 < -tiny)).any(axis=1)
    # a corner on the open segment: look just before and after it
    length = np.hypot(r[:, 0], r[:, 1])[:, None]
    rel = corners[None] - origins[:, None]  # (k, c, 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        along = (rel * r[:, None]).sum(-1) / length**2
        off = np.abs(_cross(r[:, None], rel)) / length
    on = (off < 1e-6) & (along > 1e-9) & (along < 1 - 1e-9) & ~hidden[:, None]
    if on.any():
        ki, ci = np.nonzero(on)
        unit = r[ki] / length[ki]
        probe = 1e-4  # m: far less than any element, more than rounding
        out = ~_inside(corners[ci] - unit * probe, edges) | ~_inside(
            corners[ci] + unit * probe, edges
        )
        hidden[ki[out]] = True
    # from a node on the outline a segment may run outside without crossing anything
    if on_outline.any():
        k = np.flatnonzero(on_outline & ~hidden)
        hidden[k] = ~_inside(origins[k] + r[k] / 2, edges)
    return hidden


def _inside(points: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Even-odd test against the slab outline (holes included); a point on the outline
    (a line along a slab edge) is inside."""
    a, b = edges[:, 0], edges[:, 1]
    ab = b - a
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.clip(
            ((points[:, None, :] - a[None]) * ab[None]).sum(-1) / (ab * ab).sum(-1)[None], 0, 1
        )
    gap = np.linalg.norm(points[:, None, :] - (a[None] + k[..., None] * ab[None]), axis=-1)
    on_edge = (gap < 1e-6).any(axis=1)
    x, y = points[:, None, 0], points[:, None, 1]
    straddles = (a[None, :, 1] > y) != (b[None, :, 1] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        cross_x = a[None, :, 0] + (y - a[None, :, 1]) * (b[None, :, 0] - a[None, :, 0]) / (
            b[None, :, 1] - a[None, :, 1]
        )
    return on_edge | ((np.count_nonzero(straddles & (x < cross_x), axis=1) % 2) == 1)
