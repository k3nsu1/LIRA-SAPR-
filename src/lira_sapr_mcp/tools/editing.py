"""Tools: high-level model edits (nodes, elements, stiffness, restraints, self weight).

They change the model in LIRA (not the file): save with lira_save_model, then analyse.
The model file is backed up before the session first changes it.
"""

from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services import operations as ops
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import (
    PARTS_HELP,
    backup_before_change,
    extend_doc,
    lira_errors,
    needs,
    numbers_in,
)


def register(mcp: MCPServer, session: LiraSession) -> None:
    async def edit(model: str | None, fn: Any, *args: Any) -> tuple[str, Any, str | None]:
        ref = await session.resolve(model)
        backup = await backup_before_change(session, ref)
        return ref, await session.run(fn, session.adapter, ref, *args), backup

    async def numbers_of(model: str | None, value: list[int] | str, what: str) -> list[int]:
        numbers = await numbers_in(session, await session.resolve(model), value, what, True)
        if not numbers:
            raise LiraInputError(f'{what}: give numbers, e.g. [1, 2] or "1-10 15"')
        return numbers

    @mcp.tool()
    @lira_errors
    async def lira_add_nodes(
        coordinates: list[list[float]], model: str | None = None
    ) -> dict[str, Any]:
        """Add nodes [[X, Y, Z], ...] (in LIRA's geometry unit) after the last node;
        returns their numbers."""
        ref, nodes, backup = await edit(model, ops.add_nodes, coordinates)
        return {"model": ref, "nodes": nodes, "backup": backup}

    @mcp.tool()
    @lira_errors
    async def lira_add_elements(
        nodes: list[list[int]],
        fe_type: int = 10,
        stiffness: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Add elements of one FE type after the last element; returns their numbers.

        nodes: one node list per element, e.g. [[1, 2], [2, 3]] for bars. fe_type: LIRA's
        FE type code (10 universal space bar, 11 plate, 41/44 shells; see resource
        lira://reference/tables). stiffness: a stiffness number from lira_model_summary;
        without it the elements have no stiffness and the analysis will fail. LIRA cannot
        remove elements through its API: if it rejects the stiffness (e.g. a plate's for
        a bar), the error names the elements left without one.
        """
        ref, elements, backup = await edit(model, ops.add_elements, fe_type, nodes, stiffness)
        return {"model": ref, "elements": elements, "backup": backup}

    @mcp.tool()
    @lira_errors
    @extend_doc(PARTS_HELP)
    async def lira_set_stiffness(
        elements: list[int] | str, stiffness: int, model: str | None = None
    ) -> dict[str, Any]:
        """Assign a stiffness (number from lira_model_summary) to elements, given as
        [1, 5] or "1-10 15". To make a group stiffer or softer, create a stiffness with
        lira_copy_stiffness and assign it here."""
        numbers = await numbers_of(model, elements, "elements")
        ref, _, backup = await edit(model, ops.set_stiffness, numbers, stiffness)
        return {"model": ref, "elements": len(numbers), "backup": backup}

    @mcp.tool()
    @lira_errors
    async def lira_copy_stiffness(
        source: int, changes: dict[str, float], model: str | None = None
    ) -> dict[str, Any]:
        """Create a stiffness as a copy of stiffness `source` with some parameters changed,
        named as in the Stiffnesses table: e.g. {"B": 80, "H": 60} for a rectangle (cm),
        "E" (t/m2), "Ro" (density, t/m3). LIRA derives areas and inertias again. Returns
        the new number; assign it with lira_set_stiffness. Stiffnesses already assigned
        to elements are not rewritten; the call checks that none of them changed.
        """
        ref, number, backup = await edit(model, ops.copy_stiffness, source, changes)
        return {"model": ref, "stiffness": number, "backup": backup}

    @mcp.tool()
    @lira_errors
    @extend_doc(PARTS_HELP)
    async def lira_set_restraints(
        nodes: list[int] | str, directions: list[str], model: str | None = None
    ) -> dict[str, Any]:
        """Restrain nodes: the given directions (X Y Z UX UY UZ) become fixed and the
        others free; [] frees the nodes. E.g. ["X", "Y", "Z", "UX", "UY", "UZ"] fixes."""
        numbers = await numbers_of(model, nodes, "nodes")
        ref, _, backup = await edit(model, ops.set_restraints, numbers, directions)
        return {"model": ref, "nodes": len(numbers), "backup": backup}

    @mcp.tool()
    @lira_errors
    @needs(session, "SetDeadWeightLoad", "Select")
    @extend_doc(PARTS_HELP)
    async def lira_self_weight(
        load_case: int,
        safety_factor: float = 1.0,
        elements: list[int] | str | None = None,
        delete_all: bool = False,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Add self weight loads to load_case (LIRA creates the case if needed) for all
        elements, or only `elements`, whose sections have a density, multiplied by
        safety_factor. delete_all=true removes all self weight loads instead (load_case is
        then ignored). Check the loads with lira_read_loads (type 16).
        """
        ref = await session.resolve(model)
        numbers = None if delete_all else await numbers_in(session, ref, elements, "elements", True)
        backup = await backup_before_change(session, ref)
        if delete_all:
            message = await session.run(session.adapter.delete_self_weight, ref)
        else:
            message = await session.run(
                session.adapter.self_weight, ref, load_case, safety_factor, numbers
            )
        return {"model": ref, "message": message or None, "backup": backup}
