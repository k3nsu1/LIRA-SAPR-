"""Tool: utilization of steel sections from LIRA's steel check (.rez), as facts."""

import asyncio
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing import steel
from lira_sapr_mcp.services import results as rs
from lira_sapr_mcp.services import steel as ss
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import READ_ONLY, lira_errors, numbers_in, part_of


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_steel_check(
        model: str | None = None,
        rez_path: str | None = None,
        design_option: int = 1,
        elements: list[int] | str | None = None,
        stiffnesses: list[int] | None = None,
        sort_by: str = "max",
        top: int | None = 20,
        export_path: str | None = None,
    ) -> dict[str, Any]:
        """Utilization percentages of steel bars from LIRA's steel check (LIR-STK), read
        from the .rez file LIRA writes with the analysis: per element of the assigned
        sections 1LS (1st limit state: strength and stability), 2LS (2nd: deflection,
        slenderness) and LB (local buckling), as LIRA wrote them (strings, not recalculated
        or rounded; "NR" or a value of 1e20 and above: LIRA has no result). over_100 lists
        the elements where a percentage is above 100 - a fact, not a verdict.

        rez_path: an absolute .rez path; otherwise the file of the model (default: the
        active one) in LIRA's work folder, <model>.rez for design_option 1,
        <model>#1.rez for 2 and so on. elements (numbers, ranges, "selected", "visible")
        and stiffnesses (numbers, taken from the assigned section names "1.1.1. ...")
        narrow the elements, their union when both. groups: per stiffness (or per
        section, when its name has no stiffness number) the largest value of each measure
        and its element. sort_by: "max" (largest of the three, default), "1LS", "2LS" or
        "LB"; top: rows shown (null: all). export_path (absolute .csv or .xlsx): all rows
        and the groups.
        """
        if rez_path is not None:
            path = Path(rez_path)
            if not path.is_absolute():
                raise LiraInputError("rez_path: an absolute path to a .rez file")
            warning = None
            ref = model
        else:
            ref = model or await session.resolve(None)
            info = await session.run(session.adapter.document_info, ref)
            dirs = await session.run(session.adapter.directories)
            path, warning = ss.find_rez(
                dirs.get("WorkDirectory"), info.path or info.title, design_option
            )
        within = None
        if elements is not None:
            if ref is None and part_of(elements):
                ref = await session.resolve(None)  # "selected" / "visible": of the open model
            within = await numbers_in(session, ref or "", elements, "elements")
        try:
            file = await asyncio.to_thread(steel.load, path)
        except steel.RezMissing as exc:
            raise LiraInputError(f"{exc}: {exc.filename}") from exc
        except steel.RezError as exc:
            raise LiraInputError(str(exc)) from exc
        result = await asyncio.to_thread(ss.check, file, within, stiffnesses, sort_by, top)
        rows = result.pop("_all_rows")
        result = {"rez": file.path, "encoding": file.encoding_name} | result
        if warning:
            result["warning"] = warning
        if export_path:
            groups = [
                {k: v for k, v in g.items() if not isinstance(v, dict)}
                | {f"{m}_{k}": v for m in steel.MEASURES for k, v in g[m].items()}
                for g in result["groups"]
            ]
            flat = [r | {"over_100": " ".join(r["over_100"])} for r in rows]
            for g in groups:
                g["sections"] = "; ".join(g["sections"])
            result["export"] = await asyncio.to_thread(
                rs.export, {"elements": flat, "groups": groups}, {}, export_path
            )
        return result
