"""Tools: which elements and nodes the user pointed at in LIRA's window."""

from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import format_numbers
from lira_sapr_mcp.services.elements import Criteria, find_elements
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import (
    PARTS,
    PARTS_HELP,
    READ_ONLY,
    extend_doc,
    lira_errors,
    needs,
    numbers_in,
)

# Ranges the agent has to pass on to the next tool: past this the answer gets too long.
MAX_RANGES_CHARS = 20_000


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @needs(session, "GetSelection")
    async def lira_selection(model: str | None = None, part: str = "selected") -> dict[str, Any]:
        """Elements and nodes the user selected in LIRA's window (part "selected") or
        that LIRA shows (part "visible": after a fragmentation, only the fragment) in an
        open model (default: the active one), as LIRA-style ranges with their counts.
        Tools that take node or element numbers also accept "selected" and "visible"
        directly. Show the user the counts before changing what they pointed at.
        """
        if part not in PARTS:
            raise LiraInputError(f"part must be one of {', '.join(PARTS)}, not {part!r}")
        ref = await session.resolve(model)
        found = await session.run(session.adapter.selection, ref, part)
        result: dict[str, Any] = {"model": ref, "part": part}
        for kind in ("elements", "nodes"):
            result[kind] = format_numbers(found[kind])
            result[f"{kind}_count"] = len(found[kind])
        return result

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(PARTS_HELP)
    async def lira_find_elements(
        model: str | None = None,
        fe_types: list[int] | None = None,
        stiffnesses: list[int] | None = None,
        z: list[float] | None = None,
        tolerance: float = 0.001,
        block_kind: str | None = None,
        floor: str | None = None,
        mark: str | None = None,
        material: str | None = None,
        within: list[int] | str | None = None,
    ) -> dict[str, Any]:
        """Elements of an open model (default: the active one) that meet every given
        criterion, for requests in words such as "the plates at +15.000" or "the walls
        of stiffness 7". Returns their numbers as LIRA-style ranges, the count per FE
        type and per stiffness, and the Z range of their nodes: show the user the
        counts before changing the elements, then pass the ranges to other tools.

        fe_types: FE type codes (10 bar, 41/44 shells, ...: resource lira://reference/
        tables). stiffnesses: stiffness numbers. z: [Z] or [Z min, Z max] in LIRA's
        geometry unit; every node of an element must lie there, +/- tolerance (plates
        of a slab at one level; columns of a storey between two levels). block_kind,
        floor, mark: of LIRA's structural blocks (table StructuralBlocks; kind e.g.
        ПЛИТА, СТЕНА, КОЛОННА, БАЛКА), compared without case. material: design material
        of Elements_Materials, "RC" or with its components "RC 1.2.1". within: search
        only among these elements, e.g. "visible" after a fragmentation.
        """
        ref = await session.resolve(model)
        among = await numbers_in(session, ref, within, "elements") if within else None
        if z is not None and len(z) not in (1, 2):
            raise LiraInputError("z: [Z] or [Z min, Z max]")
        criteria = Criteria(
            fe_types=tuple(fe_types or ()),
            stiffnesses=tuple(stiffnesses or ()),
            z=(min(z), max(z)) if z else None,
            tolerance=tolerance,
            block_kind=block_kind,
            floor=floor,
            mark=mark,
            material=material,
            within=tuple(among or ()),
        )
        found = await session.run(
            find_elements,
            session.adapter,
            ref,
            criteria,
            timeout=session.settings.call_timeout_s * 5,
        )
        numbers = found.pop("elements")
        ranges = format_numbers(numbers)
        if len(ranges) > MAX_RANGES_CHARS:
            raise LiraInputError(
                f"{len(numbers)} elements found, scattered over {ranges.count(' ') + 1} "
                "ranges: too many to pass on. Narrow the criteria (e.g. add z or "
                f"fe_types, or search within a fragment). Found: {found}"
            )
        return {"model": ref, "count": len(numbers), "elements": ranges} | found
