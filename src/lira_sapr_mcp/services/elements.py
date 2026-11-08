"""Finding elements by what they are and where they lie (run on the COM thread).

The engineer describes elements in words ("the plates at +15.000", "the walls of
stiffness 7"); the agent turns that into criteria, and this module reads the input
tables once and keeps the elements that meet all of them.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import parse_numbers


@dataclass(frozen=True)
class Criteria:
    fe_types: tuple[int, ...] = ()
    stiffnesses: tuple[int, ...] = ()
    z: tuple[float, float] | None = None  # every node of the element within, ± tolerance
    tolerance: float = 0.001
    block_kind: str | None = None  # StructuralBlocks: kind, floor, mark (case-insensitive)
    floor: str | None = None
    mark: str | None = None
    material: str | None = None  # Elements_Materials: "RC", or with components "RC 1.2.1"
    within: tuple[int, ...] = ()  # only among these elements

    def empty(self) -> bool:
        return not (
            self.fe_types
            or self.stiffnesses
            or self.z
            or self.block_kind
            or self.floor
            or self.mark
            or self.material
            or self.within
        )


def _nodes_of(cell: Any) -> list[int]:
    """Nodes of an element as LIRA writes them: "1, 4" (or a single number)."""
    if cell is None:
        return []
    return [int(p) for p in str(cell).replace(",", " ").split()]


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip().casefold()


def _in_blocks(rows: list[list], c: Criteria) -> set[int]:
    """Elements of the structural blocks whose kind, floor and mark match."""
    found: set[int] = set()
    for row in rows:
        cells = [*row, *[None] * 6][:6]
        elements, kind, _, floor, mark = cells[:5]
        if c.block_kind and _text(kind) != _text(c.block_kind):
            continue
        if c.floor and _text(floor) != _text(c.floor):
            continue
        if c.mark and _text(mark) != _text(c.mark):
            continue
        found.update(parse_numbers(str(elements or "")) or [])
    return found


def _with_material(rows: list[list], wanted: str) -> set[int]:
    parts = wanted.split()
    code, components = parts[0].casefold(), (parts[1] if len(parts) > 1 else None)
    found = set()
    for row in rows:
        if len(row) < 2 or _text(row[1]) != code:
            continue
        if components is None or (len(row) > 2 and str(row[2]).strip() == components):
            found.add(int(row[0]))
    return found


def find_elements(adapter: Any, ref: str, c: Criteria) -> dict[str, Any]:
    """Elements of ``ref`` that meet every criterion; with counts per FE type and
    stiffness and the Z range of their nodes, so the agent can show what it found."""
    if c.empty():
        raise LiraInputError(
            "Give at least one criterion: fe_types, stiffnesses, z, block_kind, floor, "
            "mark, material or within"
        )
    within = list(c.within) or None
    rows = adapter.read_table(ref, "Elements_TypeAndNodes", within)
    types = {int(r[0]): int(r[1]) for r in rows if r and r[0] is not None and r[1] is not None}
    keep = set(types) & set(within) if within else set(types)
    if c.fe_types:
        keep = {e for e in keep if types[e] in c.fe_types}
    if c.block_kind or c.floor or c.mark:
        keep &= _in_blocks(adapter.read_table(ref, "StructuralBlocks"), c)
    if c.material:
        keep &= _with_material(adapter.read_table(ref, "Elements_Materials", within), c.material)
    stiffness = {
        int(r[0]): int(r[1])
        for r in adapter.read_table(ref, "Elements_Stiffnesses", within)
        if r and r[0] is not None and r[1] is not None
    }
    if c.stiffnesses:
        keep = {e for e in keep if stiffness.get(e) in c.stiffnesses}
    nodes = {
        int(r[0]): _nodes_of(r[2]) for r in rows if r and r[0] is not None and int(r[0]) in keep
    }
    z_of: dict[int, float] = {}
    if keep:
        wanted = sorted({n for ns in nodes.values() for n in ns})
        for r in adapter.read_table(ref, "Nodes_Coordinates", wanted):
            if r and r[0] is not None and r[3] is not None:
                z_of[int(r[0])] = float(r[3])
    if c.z:
        low, high = min(c.z) - c.tolerance, max(c.z) + c.tolerance
        keep = {
            e
            for e in keep
            if nodes[e] and all(n in z_of and low <= z_of[n] <= high for n in nodes[e])
        }
    found = sorted(keep)
    zs = [z_of[n] for e in found for n in nodes[e] if n in z_of]
    return {
        "elements": found,
        "by_fe_type": dict(sorted(Counter(types[e] for e in found).items())),
        "by_stiffness": dict(sorted(Counter(stiffness.get(e, 0) for e in found).items())),
        "z_range": [min(zs), max(zs)] if zs else None,
    }
