"""Tools: LIRA input tables and loads."""

from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import format_numbers, parse_numbers
from lira_sapr_mcp.services import loads as loads_service
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.services.textformat import Load
from lira_sapr_mcp.tools.common import (
    READ_ONLY,
    backup_before_change,
    lira_errors,
    needs,
    numbers_in,
    part_of,
    plain,
)

Cell = str | int | float | None
# Input tables with a row per element: "selected" / "visible" in them mean elements.
ELEMENT_TABLES = ("elements_", "bars_", "plates_", "solids_", "special_", "stiffnesses_factors")
# Bars_InternalForces may not start with the element (its layout is unconfirmed): words
# are refused there rather than filtering by the wrong column.
NOT_ELEMENT_TABLES = ("bars_internalforces",)
# LIRA writes the text export in the background; the adapter waits up to 120 s for it.
# The text of a model of 230 000 nodes takes 4 minutes; the adapter stops on its own
# when LIRA writes nothing new for 2 minutes.
EXPORT_TIMEOUT_S = 1800.0
# The rebuild of a model of 230 000 nodes and 1.5 million loads took 22 minutes.
REBUILD_TIMEOUT_S = 3600.0


def register(mcp: MCPServer, session: LiraSession) -> None:
    async def targets_of(
        ref: str,
        elements: list[int] | str | None,
        nodes: list[int] | str | None,
        words: bool = True,
    ) -> loads_service.Targets | None:
        """The elements and nodes a loads call is limited to; None for all. Without
        ``words``, "selected" / "visible" are refused."""
        if elements is None and nodes is None:
            return None
        said = [w for w in (part_of(elements), part_of(nodes)) if w]
        if said and not words:
            raise LiraInputError(
                f'Give numbers here, not "{said[0]}": take them from lira_read_loads, which '
                "names the elements and nodes it read"
            )
        return loads_service.Targets(
            frozenset(await numbers_in(session, ref, elements, "elements") or ()),
            frozenset(await numbers_in(session, ref, nodes, "nodes") or ()),
        )

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_list_input_tables() -> list[str]:
        """Names of the input tables this LIRA release supports (starts LIRA if needed).

        Typical: Nodes_Coordinates (number, X, Y, Z), Elements_TypeAndNodes (number,
        FE type, "node, node, ..."), Nodes_Restraints (node + 6 flags X Y Z UX UY UZ),
        Stiffnesses, Elements_Stiffnesses (element, stiffness number), LoadCases, DCL.
        Columns of every table, FE type codes and the text format: resource
        lira://reference/tables. Read a table first to see its value format.
        """
        await session.run(session.adapter.connect)
        return sorted(session.adapter.table_kinds)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_read_table(
        table: str,
        model: str | None = None,
        numbers: list[int] | str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Rows of an input table (default model: the active one).

        numbers: only these nodes/elements, as a list or "1-10 15"; in tables of nodes
        (Nodes_*) or of elements also "selected" or "visible" (what the user selected
        in LIRA's window, or what LIRA shows after a fragmentation). Values use LIRA's
        current units (see lira_model_summary). Empty cells are null. Large tables are
        paged with offset/limit; the result says how many rows there are in total.
        """
        ref = await session.resolve(model)
        name = table.lower()
        if name.startswith("nodes_"):
            wanted = await numbers_in(session, ref, numbers, "nodes")
        elif name.startswith(ELEMENT_TABLES) and not name.startswith(NOT_ELEMENT_TABLES):
            wanted = await numbers_in(session, ref, numbers, "elements")
        else:  # numbers of stiffnesses, load cases...: "selected" is a bad list here
            wanted = parse_numbers(numbers)
        rows = await session.run(session.adapter.read_table, ref, table, wanted)
        page = rows[offset : offset + max(1, min(limit, 5000))]
        return {"table": table, "total_rows": len(rows), "offset": offset, "rows": page}

    @mcp.tool()
    @lira_errors
    async def lira_write_table(
        table: str,
        rows: list[list[Cell]],
        model: str | None = None,
        numbers: list[int] | str | None = None,
    ) -> dict[str, Any]:
        """Write rows into an input table and apply them to the model.

        Without numbers the rows replace the whole table: rows with new numbers add
        objects (e.g. nodes). With numbers only those objects' rows are replaced (numbers
        only: the rows name their objects, so "selected" does not apply here).
        LIRA checks the rows; on any error nothing changes and the errors are returned
        with 0-based row/column. The model file is backed up before its first change.
        """
        ref = await session.resolve(model)
        backup = await backup_before_change(session, ref)
        errors = await session.run(
            session.adapter.write_table, ref, table, rows, parse_numbers(numbers)
        )
        return {"applied": not errors, "errors": plain(errors), "backup": backup}

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @needs(session, "StartCmd")
    async def lira_read_loads(
        model: str | None = None,
        load_cases: list[int] | None = None,
        elements: list[int] | str | None = None,
        nodes: list[int] | str | None = None,
    ) -> dict[str, Any]:
        """Loads of a model, from LIRA's text export.

        Each load: case, target (node number if on_node, else element number), type
        (LIRA load type code; 0 = nodal force/moment, 6 = uniform load on a bar),
        direction (1 X, 2 Y, 3 Z; nodal moments 4 UX, 5 UY, 6 UZ), values (in the
        units of the text format, see the model's document 0 key 33).
        elements / nodes: only the loads on these (a large model has millions of
        loads); also "selected" or "visible", as the user pointed at them in LIRA. The
        answer then names them as numbers: pass those to lira_set_loads. A load marked
        target_kind_unknown has a type the server cannot place on a node or an element:
        lira_set_loads then refuses to change only these elements and nodes.
        """
        ref = await session.resolve(model)
        targets = await targets_of(ref, elements, nodes)
        loads = await session.run(
            loads_service.read_loads,
            session.adapter,
            ref,
            load_cases,
            targets,
            timeout=EXPORT_TIMEOUT_S,
        )
        result: dict[str, Any] = {"loads": loads, "count": len(loads)}
        if targets is not None:
            result["elements"] = format_numbers(list(targets.elements))
            result["nodes"] = format_numbers(list(targets.nodes))
        return result

    @mcp.tool()
    @lira_errors
    @needs(session, "StartCmd", "ImportFile")
    async def lira_set_loads(
        loads: list[dict[str, Any]],
        model: str | None = None,
        load_cases: list[int] | None = None,
        pack: bool = False,
        elements: list[int] | str | None = None,
        nodes: list[int] | str | None = None,
    ) -> dict[str, Any]:
        """Replace loads of a saved model: of load_cases only, or all loads when omitted.
        With elements and/or nodes, only the loads on those are replaced and all other
        loads stay: read them with lira_read_loads given the elements and nodes, change
        them, and send them back with the numbers that answer named. loads may then only
        target those. Numbers only, not "selected": the user's selection may change in
        between, and loads on an element added to it would be removed.

        loads: [{case, target, type, direction, values}] as returned by lira_read_loads.
        LIRA has no API for loads, so the server rebuilds the model through LIRA's text
        format (construction stages included), copies every input table back, verifies
        tables, loads and the model's whole text, and only then overwrites the file
        (after a fresh backup). A large model takes minutes (about 20 for 230 000 nodes).
        An unpacked scheme (deleted nodes and elements keep their numbers) is refused:
        ask the user, and with pack=true the server only packs it (only deleted nodes
        and elements go; node and element numbers change) and saves it, without applying
        the loads; read the loads again and send them in the new numbering. If verification
        fails (e.g. models under combination norms
        other than LIRA's default SP 20.13330, such as SNiP 2.01.07-85*; nonlinear
        reinforcement data LIRA loses), the original is not changed, and the rebuilt
        model with the new loads is saved next to it as a draft <name>_кор_нагр_error.lir
        with a report <name>_кор_нагр_error.txt of the differences. Tell the user: whether
        the draft is of use is theirs to decide. Data that is neither in input tables nor
        in the text format cannot be verified: check the model in LIRA after this call.
        Load values are verified to 0.02 % (LIRA's import merges value rows closer than
        that), so a change smaller than that goes unnoticed.
        """
        ref = await session.resolve(model)
        targets = await targets_of(ref, elements, nodes, words=False)
        try:
            parsed = [
                Load(
                    int(d["case"]),
                    int(d["target"]),
                    int(d["type"]),
                    int(d["direction"]),
                    [float(v) for v in d.get("values", [])],
                )
                for d in loads
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise LiraInputError(
                f"Bad load {exc!s}: each load needs case, target, type, direction, values"
            ) from exc
        # A fresh backup each time: the rebuild (and packing) rewrites the whole file.
        backup = await backup_before_change(session, ref, fresh=True)
        result = await session.run(
            loads_service.set_loads,
            session.adapter,
            ref,
            parsed,
            load_cases,
            pack,
            targets,
            timeout=REBUILD_TIMEOUT_S,
        )
        return result | {"backup": backup}

    @mcp.tool()
    @lira_errors
    @needs(session, "ImportFile")
    async def lira_import_text_model(text: str, save_as: str) -> dict[str, Any]:
        """Create a model from LIRA's text input format and save it as save_as (.lir).

        The text format describes nodes, elements, stiffnesses, restraints and loads in
        numbered documents, e.g. ( 1/ elements ) ( 3/ stiffnesses ) ( 4/ coordinates )
        ( 5/ restraints ) ( 6/ loads ) ( 7/ load values ). The best way to learn it is to
        export a similar model with lira_export_text.
        """
        backup = session.backups.ensure(save_as)
        info = await session.run(loads_service.import_text_model, session.adapter, text, save_as)
        return {"model": plain(info), "backup": str(backup) if backup else None}

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @needs(session, "StartCmd")
    async def lira_export_text(model: str | None = None) -> dict[str, Any]:
        """The whole model in LIRA's text input format (documents 0-8 and more)."""
        ref = await session.resolve(model)
        text = await session.run(session.adapter.export_text, ref, timeout=EXPORT_TIMEOUT_S)
        return {"text": text}
