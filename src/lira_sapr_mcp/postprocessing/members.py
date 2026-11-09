"""Bar members of a frame: beams with their spans and columns with their drift.

Facts only, no verdict: Kazakhstan's SP RK EN 1993-1-1 (7.2.1, 7.2.2) and its national
annex (NP.2.21, NP.2.23) leave the limits of deflections and horizontal displacements to
each project, so the engineer sets them; here only f, the span and L / f are computed.

A member is a chain of collinear bars joined end to end: at a node the chain goes on only
when exactly one other chosen bar continues it in a straight line. A beam member is cut
into spans at its supports: a restrained node, a node with a one-node support element
(spring, pile), a node a column goes down from, a node of a rigid body or coupled group that
holds such a node, and the nodes it shares with elements given as supporting (a purlin rests
on rafters: the rafters are its supports). Parts beyond the first and the last support are
cantilevers. A member with no support is taken as resting on its two ends.

The deflection of a span is the displacement of each inner node minus the displacement of
the straight line through its two supports at that point, without the part along the bar;
of a cantilever, the displacement of its free end minus that of its support. Only nodes
are read: a span of one finite element has no inner node and its deflection is not known
(the note says so; split the bar in LIRA). A column's drift is the horizontal displacement
of its top relative to its base, and per part between the nodes where other bars join it.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from math import cos, radians

import numpy as np

# Bars: plane and space frames, trusses, the universal bar (10), with their non-linear 2xx
# analogues. Springs and one-node supports (51-57) are not bars.
BAR_TYPES = frozenset({1, 2, 3, 4, 5, 6, 7, 10})
ONE_NODE_SUPPORTS = frozenset({51, 53, 54, 56, 57})  # spring, damper-like, pile: hold a node
STRAIGHT = cos(radians(2.0))  # two bars continue a member when they deviate less than this
UPRIGHT = cos(radians(15.0))  # a member closer than this to the vertical is a column
SAME = 1e-6  # m: lengths shorter than this are nothing


def is_bar(fe_type: int) -> bool:
    t = fe_type - 200 if 200 < fe_type < 300 else fe_type
    return t in BAR_TYPES


@dataclass(frozen=True)
class Member:
    """Collinear bars in a row: the elements and their nodes from one end to the other."""

    elements: tuple[int, ...]
    nodes: tuple[int, ...]
    column: bool

    @property
    def ends(self) -> tuple[int, int]:
        return self.nodes[0], self.nodes[-1]


@dataclass(frozen=True)
class Span:
    """A part of a beam between two supports (``cantilever`` False) or from a support to a
    free end (True, ``nodes[0]`` is the support). ``nodes`` run along the member."""

    member: int
    nodes: tuple[int, ...]
    length: float
    cantilever: bool


@dataclass(frozen=True)
class SpanDeflection:
    """The largest deflection of a span: at ``node``, ``f`` (m, across the bar), its vertical
    part ``fz`` with LIRA's sign (minus is down), ``ratio`` = length / f (inf when f is 0).
    ``node`` is None when the span has no inner node to read."""

    span: Span
    node: int | None
    f: float
    fz: float
    ratio: float


@dataclass(frozen=True)
class Drift:
    """A column's horizontal displacement ``u`` (m) of its top relative to its base over the
    height ``h``; ``parts``: (bottom node, top node, height, u) between the nodes where other
    bars join it."""

    member: int
    base: int
    top: int
    h: float
    u: float
    parts: list[tuple[int, int, float, float]] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.h / self.u if self.u > SAME else float("inf")


def _unit(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    v = b - a
    n = float(np.linalg.norm(v))
    return v / n if n > SAME else v


def members(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, tuple[int, tuple[int, ...]]],
    within: set[int] | None = None,
) -> list[Member]:
    """Chains of collinear bars among ``within`` (every bar when None), longest first."""
    xyz = {n: np.asarray(p, dtype=float) for n, p in nodes.items()}
    bars = {
        el: ns
        for el, (fe, ns) in elements.items()
        if is_bar(fe)
        and len(ns) == 2
        and ns[0] != ns[1]
        and (within is None or el in within)
        and ns[0] in xyz
        and ns[1] in xyz
    }
    at: dict[int, list[int]] = defaultdict(list)
    for el, (a, b) in bars.items():
        at[a].append(el)
        at[b].append(el)

    def axis(el: int, from_node: int) -> np.ndarray:
        a, b = bars[el]
        return _unit(xyz[from_node], xyz[b if a == from_node else a])

    def onward(el: int, node: int) -> int | None:
        """The one chosen bar that goes on from ``el`` through ``node`` in a straight line."""
        back = axis(el, node)  # from the node back along el
        straight = [
            o for o in at[node] if o != el and float(np.dot(axis(o, node), back)) < -STRAIGHT
        ]
        return straight[0] if len(straight) == 1 else None

    seen: set[int] = set()
    out: list[Member] = []
    for el in sorted(bars):
        if el in seen:
            continue
        a, b = bars[el]
        chain, row = [el], [a, b]
        seen.add(el)
        for forward in (True, False):  # grow from b, then from a
            cur, node = el, (row[-1] if forward else row[0])
            while (nxt := onward(cur, node)) is not None and nxt not in seen:
                seen.add(nxt)
                far = bars[nxt][1] if bars[nxt][0] == node else bars[nxt][0]
                if forward:
                    chain.append(nxt)
                    row.append(far)
                else:
                    chain.insert(0, nxt)
                    row.insert(0, far)
                cur, node = nxt, far
        direction = _unit(xyz[row[0]], xyz[row[-1]])
        out.append(Member(tuple(chain), tuple(row), abs(float(direction[2])) >= UPRIGHT))
    return sorted(out, key=lambda m: -_length(xyz, m.nodes))


def _length(xyz: dict[int, np.ndarray], row: tuple[int, ...]) -> float:
    return float(np.linalg.norm(xyz[row[-1]] - xyz[row[0]]))


def supports(
    nodes: dict[int, tuple[float, float, float]],
    elements: dict[int, tuple[int, tuple[int, ...]]],
    restrained: set[int],
    groups: list[set[int]] | None = None,
    on: set[int] | None = None,
    columns: bool = True,
) -> set[int]:
    """Nodes a beam rests on: restrained, with a one-node support, with a column going down
    (unless ``columns`` is False: then only what holds the model), tied by a rigid body or
    coupled group to such a node, or of an element in ``on``."""
    held = set(restrained)
    for el, (fe, ns) in elements.items():
        t = fe % 100
        if len(ns) == 1 and t in ONE_NODE_SUPPORTS:
            held.add(ns[0])
        elif columns and is_bar(fe) and len(ns) == 2:
            a, b = (np.asarray(nodes[n], dtype=float) for n in ns)
            d = b - a
            length = float(np.linalg.norm(d))
            if length > SAME and abs(d[2]) / length >= UPRIGHT:
                held.add(ns[1] if d[2] > 0 else ns[0])  # the upper node rests on the column
        if on and el in on:
            held.update(ns)
    for g in groups or []:
        if g & held:
            held |= g
    return held


def spans(
    member_list: list[Member],
    nodes: dict[int, tuple[float, float, float]],
    held: set[int],
) -> tuple[list[Span], list[int]]:
    """Spans and cantilevers of the beams; and the members (indices) with no support,
    taken as resting on their two ends."""
    xyz = {n: np.asarray(p, dtype=float) for n, p in nodes.items()}
    out: list[Span] = []
    unsupported: list[int] = []
    for k, m in enumerate(member_list):
        if m.column:
            continue
        row = m.nodes
        cuts = [i for i, n in enumerate(row) if n in held]
        if not cuts:
            unsupported.append(k)
            cuts = [0, len(row) - 1]
        for i, j in pairwise(cuts):
            if j > i:
                part = row[i : j + 1]
                out.append(Span(k, part, _length(xyz, part), False))
        if cuts[0] > 0:
            part = tuple(reversed(row[: cuts[0] + 1]))
            out.append(Span(k, part, _length(xyz, part), True))
        if cuts[-1] < len(row) - 1:
            part = row[cuts[-1] :]
            out.append(Span(k, part, _length(xyz, part), True))
    return [s for s in out if s.length > SAME], unsupported


def span_deflection(
    span: Span,
    nodes: dict[int, tuple[float, float, float]],
    displacement: dict[int, tuple[float, float, float]],
) -> SpanDeflection:
    """The largest deflection of a span from the X, Y, Z displacements (m) of its nodes."""
    p = {n: np.asarray(nodes[n], dtype=float) for n in span.nodes}
    d = {n: np.asarray(displacement[n], dtype=float) for n in span.nodes}
    a, b = span.nodes[0], span.nodes[-1]
    e = _unit(p[a], p[b])
    if span.cantilever:
        inner = [b]
        base = {b: d[a]}
    else:
        inner = list(span.nodes[1:-1])
        base = {}
        for n in inner:
            t = float(np.dot(p[n] - p[a], e)) / span.length
            base[n] = d[a] + t * (d[b] - d[a])
    best: tuple[float, float, int] | None = None
    for n in inner:
        r = d[n] - base[n]
        r = r - float(np.dot(r, e)) * e  # only across the bar
        f = float(np.linalg.norm(r))
        if best is None or f > best[0]:
            best = (f, float(r[2]), n)
    if best is None:
        return SpanDeflection(span, None, float("nan"), float("nan"), float("nan"))
    f, fz, n = best
    return SpanDeflection(span, n, f, fz, span.length / f if f > SAME else float("inf"))


def drift(
    k: int,
    member: Member,
    nodes: dict[int, tuple[float, float, float]],
    displacement: dict[int, tuple[float, float, float]],
    joined: set[int],
) -> Drift:
    """A column's drift; ``joined``: nodes where other bars or plates meet it (storeys)."""
    row = sorted(member.nodes, key=lambda n: nodes[n][2])
    base, top = row[0], row[-1]

    def u(lo: int, hi: int) -> float:
        dx = np.asarray(displacement[hi], dtype=float) - np.asarray(displacement[lo], dtype=float)
        return float(np.hypot(dx[0], dx[1]))

    marks = [n for n in row if n in joined or n in (base, top)]
    parts = [(lo, hi, nodes[hi][2] - nodes[lo][2], u(lo, hi)) for lo, hi in pairwise(marks)]
    return Drift(k, base, top, nodes[top][2] - nodes[base][2], u(base, top), parts)


def joints(member_list: list[Member], elements: dict[int, tuple[int, tuple[int, ...]]]) -> set[int]:
    """Nodes where an element that is not part of the same member meets it."""
    owner: dict[int, int] = {}
    for k, m in enumerate(member_list):
        for el in m.elements:
            owner[el] = k
    count: dict[int, set] = defaultdict(set)
    for el, (_, ns) in elements.items():
        for n in ns:
            count[n].add(owner.get(el, ("other", el)))
    return {n for n, who in count.items() if len(who) > 1}
