"""Tools: analysis results, reduced on the server to what the agent asks.

Every result tool takes the same query arguments (see QUERY_HELP) and runs them through
one path: convert units -> envelope -> threshold -> extremes and top rows -> export.
"""

from typing import Any, NamedTuple

from mcp.server import MCPServer

from lira_sapr_mcp.adapters.results import FRAGMENT_KIND, PUNCHING_KIND, force_kinds
from lira_sapr_mcp.services import results as rs
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import READ_ONLY, extend_doc, lira_errors, numbers_in

QUERY_HELP = """
Query arguments (all result tools): components - which values (default all);
sort_by - a component to order rows by, by absolute value unless by_abs=false;
top - rows to return (at least 1, default 20, null = up to 500); envelope=true - per
place (node, or element and section) the minimum and maximum of each component over
load cases / combinations / shapes / limit states, with where each occurs (sorting and
threshold then use the one larger in magnitude); threshold - keep only rows whose
sort_by value reaches it, in the units of the answer (by absolute value unless
by_abs=false); export_path - absolute .csv or .xlsx path: the whole table after
envelope and threshold is written there (units in column headers; an existing file is
replaced) and the answer names the file. The answer always has the extremes of each
component with their place, and total_rows. nodes / elements also take "selected" or
"visible" (what the user selected in LIRA's window, or what LIRA shows after a
fragmentation): the model must then be open in LIRA, and LIRA closes it after the
analysis, so reopen it first.
"""

# Reinforcement comes in fixed units, whatever LIRA's settings.
REINFORCEMENT_UNITS = {
    "bar": {**dict.fromkeys(["AU1", "AU2", "AU3", "AU4", "AS1", "AS2", "AS3", "AS4",
                             "ASW1", "ASW2"], "cm2"), "PERC": "%",
            "crack_short": "mm", "crack_long": "mm"},
    "plate": {**dict.fromkeys(["AS1", "AS2", "AS3", "AS4", "ASW1", "ASW2"], "cm2/m"),
              **dict.fromkeys(["PercX", "PercY", "PercXY"], "%"),
              "crack_short": "mm", "crack_long": "mm"},
}  # fmt: skip


class Query(NamedTuple):
    components: list[str] | None
    sort_by: str | None
    by_abs: bool
    top: int | None
    threshold: float | None
    envelope: bool
    export_path: str | None


def register(mcp: MCPServer, session: LiraSession) -> None:
    async def model_path(model: str | None) -> str:
        """A .lir path as given (the model may be closed: LIRA closes it after the
        analysis), else an open model, else the model analysed last."""
        if model and model.lower().endswith(".lir"):
            return model
        if model is None:
            last = (await session.run(session.adapter.analysis_status))["model"]
            if last:
                return last
        return await session.resolve(model)

    async def lira_units() -> dict[str, str]:
        u = await session.run(session.adapter.units)
        return {
            "length": u.displacements,
            "force": u.forces,
            "moment_length": u.forces_per,
            "stress": u.stresses,
            "stress_length": u.stresses_per,
        }

    async def finish(
        path: str,
        tables: dict[str, list[dict]],
        units_of: dict[str, dict[str, str]],
        q: Query,
        *,
        per_family: bool,
    ) -> dict[str, Any]:
        """Reduce converted tables to the answer. units_of: per table, component -> unit."""
        answers, exported, export_units = rs.reduce_tables(
            tables,
            units_of,
            components=q.components,
            sort_by=q.sort_by,
            by_abs=q.by_abs,
            top=q.top,
            at_least=q.threshold,
            with_envelope=q.envelope,
        )
        if per_family:
            result: dict[str, Any] = {"model": path, "families": answers}
            if not tables:
                result["note"] = "no elements of the requested families"
        else:
            result = {"model": path} | next(iter(answers.values()), rs.summarize([], []))
        if q.export_path:
            result["export"] = rs.export(exported, export_units, q.export_path)
        warning = await session.run(session.adapter.results_warning, path)
        if warning:
            result["warning"] = warning
        return result

    async def nodal_answer(
        path: str, rows: list[dict], kinds: dict[str, str], q: Query
    ) -> dict[str, Any]:
        used = rs.convert(rows, kinds, await lira_units())
        return await finish(path, {"results": rows}, {"results": used}, q, per_family=False)

    async def forces_answer(path: str, rows: list[dict], q: Query) -> dict[str, Any]:
        """Forces per element family: each family has its own components."""
        units = await lira_units()
        tables: dict[str, list[dict]] = {}
        for row in rows:
            tables.setdefault(row.pop("family"), []).append(row)
        units_of = {f: rs.convert(t, force_kinds(f), units) for f, t in tables.items()}
        return await finish(path, tables, units_of, q, per_family=True)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_node_displacements(
        model: str | None = None,
        nodes: list[int] | str | None = None,
        load_cases: list[int] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Node displacements of load cases after lira_run_analysis.

        model: .lir path (may be closed), open model, or default the model analysed last.
        nodes: [1, 5] or "1-10 15" (default all); load_cases: default all. Components:
        X Y Z (in the displacement unit set in LIRA), UX UY UZ (rad). Dynamic load cases
        give a row per shape ("shape" = mode number) and LIRA's summary over the shapes
        ("shape": "sum", the values LIRA shows as summary).
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.node_displacements,
            path,
            await numbers_in(session, path, nodes, "nodes"),
            load_cases,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await nodal_answer(path, rows, rs.DISPLACEMENT_KIND, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_element_forces(
        model: str | None = None,
        elements: list[int] | str | None = None,
        load_cases: list[int] | None = None,
        families: list[str] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Internal forces and stresses of elements in load cases after lira_run_analysis,
        answered per element family, since each family has its own components:

        bar: N, Qy, Qz (force), Mx, My, Mz (force*length), Ry, Rz (soil), Bw, Tw;
        plate: Nx, Ny, Nz, Txy, Txz (stress), Mx, My, Mxy (force*length/length),
        Qx, Qy (force/length), Rz (soil pressure); solid: Nx, Ny, Nz, Txy, Txz, Tyz;
        special, special_58_59, special_310, special_264: their forces.
        Units are those set in LIRA and are listed per component. One row per element,
        load case, shape (dynamic load cases only: the mode number, and "sum" for LIRA's
        summary over the shapes, the values to check sections with) and design section
        (bars: section 1 = start; set the count in Elements_DesignSectionsCount).
        families: e.g. ["bar"] (default all); query arguments apply within each family.
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.element_forces,
            path,
            await numbers_in(session, path, elements, "elements"),
            load_cases,
            families,
            timeout=session.settings.call_timeout_s * 5,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await forces_answer(path, rows, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_load_combinations(model: str | None = None) -> dict[str, Any]:
        """Load combinations (DCL, РСН) of an analysed model: number, name, formula,
        special and seismic flags, and the building code they follow."""
        path = await model_path(model)
        return {"model": path} | await session.run(session.adapter.load_combinations, path)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_combination_displacements(
        model: str | None = None,
        nodes: list[int] | str | None = None,
        combinations: list[int] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Node displacements in load combinations (DCL, РСН) after lira_run_analysis.

        combinations: numbers from lira_load_combinations (default all); otherwise as
        lira_node_displacements, with "combination" in place of "case".
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.combination_displacements,
            path,
            await numbers_in(session, path, nodes, "nodes"),
            combinations,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await nodal_answer(path, rows, rs.DISPLACEMENT_KIND, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_combination_forces(
        model: str | None = None,
        elements: list[int] | str | None = None,
        combinations: list[int] | None = None,
        limit_states: list[int] | None = None,
        families: list[str] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Internal forces and stresses in load combinations (DCL, РСН).

        limit_states: 1 ultimate, 2 ultimate with long-term loads, 3 serviceability,
        4 serviceability with long-term loads (default all). One row per element,
        combination, limit state and design section; otherwise as lira_element_forces.
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.combination_forces,
            path,
            await numbers_in(session, path, elements, "elements"),
            combinations,
            families,
            limit_states,
            timeout=session.settings.call_timeout_s * 5,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await forces_answer(path, rows, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_design_forces(
        model: str | None = None,
        elements: list[int] | str | None = None,
        limit_states: list[int] | None = None,
        families: list[str] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Design combinations of forces (DCF, РСУ) after lira_run_analysis.

        One row per element, design section, limit state (as in lira_combination_forces)
        and combination "dcf", with its criterion number, internal group (A1, B1, ...),
        "load_cases" that form it (LIRA's notation: "1-3,-4" = cases 1 to 3 and case 4
        with the opposite sign) and "with": crane and/or seismic when included;
        otherwise as lira_element_forces.
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.design_forces,
            path,
            await numbers_in(session, path, elements, "elements"),
            families,
            limit_states,
            timeout=session.settings.call_timeout_s * 5,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await forces_answer(path, rows, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_periods(
        model: str | None = None, load_cases: list[int] | None = None
    ) -> dict[str, Any]:
        """Modes of the dynamic load cases (default all): per mode the period (s),
        frequency (Hz and rad/s), modal mass and accumulated modal mass (% of the total)
        and participation factor; "type" is LIRA's dynamic load type code (e.g. 35, 100).
        """
        path = await model_path(model)
        cases = await session.run(session.adapter.periods, path, load_cases)
        return {"model": path, "load_cases": cases}

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_reinforcement(
        model: str | None = None,
        elements: list[int] | str | None = None,
        design_option: int = 1,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Reinforcement selected by LIRA's reinforced concrete design (design_option:
        LIRA's design variant, default 1). Fixed units: areas cm2 (plates cm2 per m),
        PERC/Perc* %, crack widths mm.

        bar rows: element, design section, "symmetric" (LIRA designs both an asymmetric and
        a symmetric variant when asked), AU1-AU4 (corner bars), AS1-AS4 (bars along the
        faces), PERC, ASW1, ASW2 (stirrups), crack_short, crack_long. plate rows: AS1-AS4
        (AS1, AS2 along local X bottom/top, AS3, AS4 along Y), PercX, PercY, PercXY, ASW1,
        ASW2, crack_short, crack_long. "errors" lists values LIRA could not select.
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.reinforcement,
            path,
            await numbers_in(session, path, elements, "elements"),
            design_option,
            timeout=session.settings.call_timeout_s * 5,
        )
        tables: dict[str, list[dict]] = {}
        for row in rows:
            tables.setdefault(row.pop("family"), []).append(row)
        failed = {
            f: [{"element": r["element"], **r.pop("errors")} for r in t if "errors" in r]
            for f, t in tables.items()
        }
        q = Query(components, sort_by, by_abs, top, threshold, False, export_path)
        units_of = {f: REINFORCEMENT_UNITS[f] for f in tables}
        result = await finish(path, tables, units_of, q, per_family=True)
        for family, errors in failed.items():
            if errors:
                result["families"][family]["errors"] = errors[:50]
        if not tables:
            result["note"] = "no reinforced concrete bars or plates among these elements"
        return result

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_fragment_loads(
        model: str | None = None,
        nodes: list[int] | str | None = None,
        load_cases: list[int] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Loads on fragment: forces RX, RY, RZ, moments RUX, RUY, RUZ and bimoment RBW
        that the fragment defined in LIRA transfers to its nodes, per node and load case.
        A dynamic load case gives a row per mode shape and its summary row, shape "sum".
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.fragment_loads,
            path,
            await numbers_in(session, path, nodes, "nodes"),
            load_cases,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await nodal_answer(path, rows, FRAGMENT_KIND, q)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    @extend_doc(QUERY_HELP)
    async def lira_punching_loads(
        model: str | None = None,
        nodes: list[int] | str | None = None,
        load_cases: list[int] | None = None,
        components: list[str] | None = None,
        sort_by: str | None = None,
        by_abs: bool = True,
        top: int | None = 20,
        threshold: float | None = None,
        envelope: bool = False,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Loads on punching shear contours around nodes (N, QX, QY forces; MX, MY, MZ
        moments) per node and load case, for the nodes of contours defined in LIRA. A
        dynamic load case gives a row per mode shape and its summary row, shape "sum".
        LIRA computes these loads only in its punching shear check: run
        lira_punching_check after each lira_run_analysis (loads from a check run before
        the last analysis are refused as outdated).
        """
        path = await model_path(model)
        rows = await session.run(
            session.adapter.punching_loads,
            path,
            await numbers_in(session, path, nodes, "nodes"),
            load_cases,
        )
        q = Query(components, sort_by, by_abs, top, threshold, envelope, export_path)
        return await nodal_answer(path, rows, PUNCHING_KIND, q)
