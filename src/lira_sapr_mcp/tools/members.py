"""Tools: deflections of beams and drift of columns of bar frames (facts for the RPZ)."""

import asyncio
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services import members as sm
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import lira_errors, numbers_in


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool()
    @lira_errors
    async def lira_member_deflections(
        model: str | None = None,
        load_case: int | None = None,
        combination: int | None = None,
        elements: list[int] | str | None = None,
        supported_by: list[int] | str | None = None,
        output_dir: str | None = None,
        pictures: bool = False,
        view: str = "iso",
    ) -> dict[str, Any]:
        """Deflections of beams and drift of columns of a bar frame (FE 1-10), as facts:
        no verdict. In Kazakhstan the limits are set per project (SP RK EN 1993-1-1 7.2.1,
        7.2.2; national annex NP.2.21, NP.2.23): give the engineer f, L and L/f and let them
        compare.

        Collinear bars joined end to end make a member; a beam member is cut into spans at
        its supports: restrained nodes, one-node supports and piles, nodes a column goes down
        from, rigid bodies and coupled groups holding such a node, and every node of the
        elements in supported_by (a purlin resting on rafters: give the rafters). Parts beyond
        the outer supports are cantilevers (measured from their support). A beam with no
        support rests on its ends (notes say so). The deflection f of a span is the largest
        displacement of an inner node across the bar relative to the straight line between its
        supports (settlement and tilt of the supports are not deflection); fz is its vertical
        part with LIRA's sign. A span of a single finite element has no inner node: its
        deflection is unknown (blind_spans, notes). A column (within 15 degrees of vertical):
        horizontal displacement u of its top relative to its base, H/u, and the worst part
        between the nodes where other elements join it (worst_storey).

        elements: the bars to read (numbers, ranges, "selected", "visible"; default all bars).
        Give exactly one of load_case (static) or combination (DCL number from
        lira_load_combinations; the normative combinations are the usual ones). Answer: the
        10 worst spans (smallest L/f) and columns (smallest H/u), totals and notes.
        output_dir (absolute folder): also write <model>_members.xlsx with all of them and
        <model>_members.docx with the tables for the RPZ (existing files are replaced).
        pictures=true (needs output_dir): PNG for the RPZ appendices, A4 landscape, 200 dpi:
        <model>_scheme_<view>.png (bars by stiffness, supports), _deformed_<view>.png (the
        displaced scheme, enlarged so the largest displacement is 5 % of the model, with that
        node signed) and _spans_<view>.png (spans coloured by L/f bands, the 3 worst signed;
        the colours are for reading only, the limit is the project's). view: "iso" (default),
        "xz", "yz" or "xy".
        """
        if (load_case is None) == (combination is None):
            raise LiraInputError("Give exactly one of load_case or combination")
        if view not in ("iso", "xz", "yz", "xy"):
            raise LiraInputError('view: one of "iso", "xz", "yz", "xy"')
        out_dir = None
        if pictures and output_dir is None:
            raise LiraInputError("pictures need output_dir: the folder to write them into")
        if output_dir is not None:
            out_dir = Path(output_dir)
            if not out_dir.is_absolute():
                raise LiraInputError("output_dir: an absolute folder path")
        ref = model or await session.resolve(None)
        within = None if elements is None else await numbers_in(session, ref, elements, "elements")
        if within is not None and not within:
            raise LiraInputError("elements: the list is empty; leave it out for all bars")
        on = (
            None
            if supported_by is None
            else await numbers_in(session, ref, supported_by, "elements")
        )
        frame = await session.run(
            sm.read_frame,
            session.adapter,
            ref,
            within,
            on,
            load_case,
            combination,
            timeout=session.settings.call_timeout_s * 5,
        )
        found = await asyncio.to_thread(sm.compute, frame)
        result: dict[str, Any] = {
            "model": frame.path,
            "source": {"load_case": load_case}
            if combination is None
            else {"combination": combination},
        } | sm.for_agent(found)
        warning = await session.run(session.adapter.results_warning, frame.path)
        if warning:
            result["warning"] = warning
        if out_dir is not None:
            source = f"загружение {load_case}" if combination is None else f"РСН {combination}"
            stem = Path(frame.path).stem
            result["files"] = await asyncio.to_thread(sm.write_tables, found, out_dir, stem, source)
            if pictures:
                result["files"] += await asyncio.to_thread(
                    sm.pictures, frame, out_dir, stem, source, view
                )
        return result
