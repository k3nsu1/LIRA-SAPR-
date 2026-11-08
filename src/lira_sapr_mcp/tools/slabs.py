"""Tools: slabs - deflection relative to supports, isofields, reinforcement mosaics,
reports. The server reads the model once and computes on its side (decision 0003): the
agent gets a short summary per slab and the files."""

import asyncio
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing.mesh import LEVEL_TOLERANCE
from lira_sapr_mcp.postprocessing.settlement import (
    STRUCTURES,
    STRUCTURES_SOURCE,
    WITHIN_A_WALL_FROM,
)
from lira_sapr_mcp.services import mosaics as sm
from lira_sapr_mcp.services import slabs as ss
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import extend_doc, lira_errors, numbers_in

SLABS_HELP = """
A slab is a connected set of bending plates or shells whose nodes lie on one level;
its supports are the slab nodes held from below: columns and walls going down, Z
restraints, one-node supports and piles, rigid bodies (all degrees of freedom) and
coupled Z groups containing such a node. elements limits the search for slab plates
(numbers, LIRA-style ranges, "selected" or "visible" - e.g. lira_find_elements
results; default every level plate of the model). A slab keeps its number in the model, and
the report on chosen slabs is named with their numbers (<model>_slab7_deflections), so it does
not replace the report on the whole model. Of a slab only partly chosen, the chosen plates are
reported as a part (part_of_slab; files <model>_slab7_part_...): for deflections they count as a
slab of their own, with the edge of the choice a free edge. The model must be saved and analysed;
a .lir path is opened in LIRA if it is not open.
"""


def _checked_output(output_dir: str | None, report: str | None) -> tuple[Path | None, str | None]:
    """The folder and the report to write. A folder given means a report, unless the
    agent says "none"."""
    if report not in (None, "docx", "pdf", "both", "none"):
        raise LiraInputError('report: "docx", "pdf", "both" or "none"')
    out_dir = Path(output_dir) if output_dir else None
    if out_dir is not None and not out_dir.is_absolute():
        raise LiraInputError("output_dir must be an absolute folder path")
    if report not in (None, "none") and out_dir is None:
        raise LiraInputError("A report needs output_dir: the folder to write it into")
    if report == "none":
        return out_dir, None
    return out_dir, report or ("both" if out_dir else None)


def register(mcp: MCPServer, session: LiraSession) -> None:
    async def chosen_elements(ref: str, elements: Any, edit: bool = False) -> list[int] | None:
        """The asked elements; None: all slabs. An empty list is a mistake, not "all"."""
        if elements is None:
            return None
        found = await numbers_in(session, ref, elements, "elements", edit)
        if not found:
            raise LiraInputError("elements: the list is empty; leave it out for all slabs")
        return found

    @mcp.tool()
    @lira_errors
    @extend_doc(SLABS_HELP)
    async def lira_slab_deflections(
        model: str | None = None,
        load_case: int | None = None,
        combination: int | None = None,
        elements: list[int] | str | None = None,
        deflection_limit: float | None = None,
        structure: str | None = None,
        settlement_limit_mm: float | None = None,
        relative_settlement_limit: float | None = None,
        within_a_wall_from_m: float = WITHIN_A_WALL_FROM,
        output_dir: str | None = None,
        report: str | None = None,
        title: str | None = None,
        pictures: bool = False,
        dxf: bool = False,
    ) -> dict[str, Any]:
        """Deflection of slabs relative to their supports, one summary per slab. Kazakhstan:
        the limit of deflections is set per project (SP RK EN 1993-1-1 7.2.1, national annex
        NP.2.21); give it as deflection_limit = N of span / N (e.g. 250). Without it the answer
        is facts only: f, span and L_to_f (span / |f|), verdict "no limit set: facts only".

        A deflection is read along lines between supports, as along a line of sight: every
        pair of support points whose straight line stays inside the slab (slab and opening
        edges stop it), passes through no other support (within 100 mm) and crosses no wall,
        and only at the points of that line: the displacement of the slab at the point
        (interpolated within the element) minus that of the straight line between its ends. The
        sign is LIRA's: negative (deflection_mm) means the slab is below the line, and the
        absolute value is compared. The span is the length of the line. A free edge no such
        line comes near is a cantilever: the shortest way from the edge to a support, read at
        its end on the edge, span = twice the overhang. Each slab reports its worst line:
        the point (x_m, y_m), deflection and allowed (mm), span (m), ratio, the supports it
        runs between and the displacements behind the deflection, L_to_f; with a limit also
        allowed_mm, ratio (|f| / allowed) and the verdict; other_spans: the
        next most dangerous places, 2 of them and more when more places exceed the limit (the
        first 10 here, all of them in the report; other_spans_total says how many). panel_lows:
        the lowest point of each panel, the largest displacement there; it is not on a line,
        so it is shown, not checked (the first 5 here, all on the isofield).

        A slab with bedding coefficients C1, C2 on most of its plates (plates_on_bedding), or
        held mostly by one-node elastic supports, under all of it or under separate points such
        as piles (on_elastic_foundation; on_separate_supports for the latter): no
        spans, no deflection (a one-node element with no stiffness along Z holds nothing up and
        is no support at all: notes say how many), and instead: the settlement in LIRA's sign
        (negative is down) against settlement_limit_mm (a positive size): the size is checked,
        so a slab rising that far is over it too; with the zones over it (zones_over_limit),
        and the relative difference of settlements |s1 - s2| / L against
        relative_settlement_limit between the vertical structures standing on the slab
        (relative_settlement): the largest one while within the limit, otherwise every place
        over it. Limits by SP RK 5.01-102-2013, annex V, table V.1: structure = "rc_frame"
        (RC frame: 0.002, 100 mm), "rc_frame_tied" (with RC belts or monolithic floors, or
        monolithic: 0.003, 150), "steel_frame" (0.004, 150), "steel_frame_tied" (0.005, 180),
        "no_uneven_forces" (0.006, no settlement limit); settlement_limit_mm and
        relative_settlement_limit, when given, override it (notes 3 and 5 of the table allow
        more). No structure and no limits: facts only. A column is one point: on a rigid body its
        centre
        (an end marked rigid_body_centre; node is the nearest one), with the mean settlement of
        the body, which tilts as a whole. A wall or pylon is one structure of plates joined at
        the slab: all pairs of points between different structures, and within one structure
        only points at least within_a_wall_from_m apart (default 3). The engineer may give other
        limits. worst speaks of deflections only;
        slabs_over_limits lists every slab over any limit, foundations included.

        Give exactly one of load_case (a static one) or combination (DCL number from
        lira_load_combinations; LIRA's normative long-term combination is the usual one
        for deflections).

        output_dir (absolute folder): write a report in Russian there. report: "both"
        (default), "docx", "pdf" or "none" (<model>_slab_deflections): summary table and,
        per slab, how the worst deflection and its limit come out, a line of numbers per place
        shown, then each isofield on a page of its own, on a scale of its own, with the 3 worst
        places and every place over the limit marked by number (their lines and worst points)
        and the low point of each panel signed; an isofield shows, at every node alike, its
        displacement minus that of the surface through the supports (the plane through three of
        them, beyond the outer ones the line between the two nearest), so with every support at
        zero it equals LIRA's displacement;
        the PDF is saved by the Microsoft Word installed on this computer, or by the
        server where Word cannot (the answer's pdf_by says which and why). title: the
        report's title. pictures=true: keep the isofields as PNG too, only when the engineer
        asks (<model>_slab<N>_deflection.png, _ratio.png, _settlement.png); existing files
        are replaced. dxf=true: a drawing per slab in output_dir, when the engineer asks (files):
        a mosaic with zones by layer on the scale of the slab's isofield and each element's value
        at its centre, the mean of its nodes, so a peak at a node reads a little lower. Two plans
        of a slab that spans, <model>_slab<N>_deflection.dxf: mm and f / fu; one of a slab that
        settles, _settlement.dxf. A slab with neither has no file.
        """
        if (load_case is None) == (combination is None):
            raise LiraInputError("Give exactly one of load_case or combination")
        if deflection_limit is not None and deflection_limit <= 0:
            raise LiraInputError("deflection_limit: N of span / N, above zero (e.g. 250)")
        if structure is not None and structure not in STRUCTURES:
            raise LiraInputError(f"structure: one of {', '.join(STRUCTURES)}")
        _, table_relative, table_settlement = STRUCTURES.get(structure, ("", None, None))
        settlement = (
            settlement_limit_mm / 1000 if settlement_limit_mm is not None else table_settlement
        )
        relative = (
            relative_settlement_limit if relative_settlement_limit is not None else table_relative
        )
        if (settlement is not None and settlement <= 0) or (relative is not None and relative <= 0):
            raise LiraInputError("settlement_limit_mm and relative_settlement_limit: above zero")
        if within_a_wall_from_m < 0:
            raise LiraInputError("within_a_wall_from_m: zero or more")
        out_dir, report = _checked_output(output_dir, report)
        if pictures and out_dir is None:
            raise LiraInputError("pictures need output_dir: the folder to write them into")
        if dxf and out_dir is None:
            raise LiraInputError("dxf needs output_dir: the folder to write the drawings into")
        if out_dir is not None and not (report or pictures or dxf):
            raise LiraInputError(
                'report "none", no pictures and no dxf: nothing to write into output_dir'
            )
        ref = model or await session.resolve(None)
        within = await chosen_elements(ref, elements)
        path, slabs, ws, notes = await session.run(
            ss.read_slabs,
            session.adapter,
            ref,
            within,
            load_case,
            combination,
            LEVEL_TOLERANCE,
            timeout=session.settings.call_timeout_s * 5,
        )
        found = await asyncio.to_thread(ss.deflections, slabs, ws, deflection_limit)
        summaries = await asyncio.to_thread(
            ss.deflection_summaries, slabs, ws, found, settlement, relative,
            within_a_wall_from_m, deflection_limit,
        )  # fmt: skip
        measured = [s for s in summaries if "deflection_mm" in s]
        result: dict[str, Any] = {
            "model": path,
            "source": {"load_case": load_case}
            if combination is None
            else {"combination": combination},
            "limits": {
                "deflection": None if deflection_limit is None else f"span / {deflection_limit:g}",
                "settlement_mm": None if settlement is None else round(settlement * 1000, 1),
                "relative_settlement": relative,
                **({"structure": f"{STRUCTURES_SOURCE}: {structure}"} if structure else {}),
            },
            "slabs": [ss.for_agent(s) for s in summaries],
        }
        if measured:
            # the ratio is rounded: of equal ones, the one over the limit
            worst = max(measured, key=lambda s: (ss.severity(s), s["verdict"] != "ok"))
            result["worst"] = {
                "slab": worst["slab"],
                "L_to_f": worst["L_to_f"],
                **({"ratio": worst["ratio"]} if "ratio" in worst else {}),
                "verdict": worst["verdict"],
            }
        over = [s["slab"] for s in summaries if s["verdict"].startswith("exceeds")]
        if over:  # worst above speaks of deflections only, this of foundations too
            result["slabs_over_limits"] = over
        if notes:
            result["notes"] = [n.text for n in notes]
        warning = await session.run(session.adapter.results_warning, path)
        if warning:
            result["warning"] = warning
        if out_dir is not None:
            for_report = [n.ru for n in notes] + ([ss.STALE_RESULTS] if warning else [])
            source = f"загружение {load_case}" if combination is None else f"РСН {combination}"
            limits = (
                "не задан для проекта (СП РК EN 1993-1-1, п. 7.2.1; НП.2.21) — приведены f, l и l/f"
                if deflection_limit is None
                else f"l/{deflection_limit:g}, задан для проекта (СП РК EN 1993-1-1, п. 7.2.1)"
            )
            result["files"] = await asyncio.to_thread(
                ss.deflection_files, path, slabs, ws, found, summaries, out_dir, report,
                source, limits, title, for_report, pictures, deflection_limit, within is not None,
                dxf,
            )  # fmt: skip
        return result

    @mcp.tool()
    @lira_errors
    async def lira_cracked_copy(
        factor: float,
        model: str | None = None,
        elements: list[int] | str | None = None,
        save_as: str | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        """A copy of a saved model with cracks in its slabs taken into account: the
        factor kE (0 < kE <= 1, the engineer gives it, e.g. 0.3) to the elastic modulus
        of every slab plate (level bending plates and shells; or of `elements`:
        numbers, ranges, "selected" or "visible" of the open model), written to
        Stiffnesses_Factors of the copy. The model itself does not change. The copy is
        <model>_kE<factor>.lir beside it, the dot written as _ (kE 0.5: _kE0_5; LIRA
        refuses . # * ? , in model names), or save_as, an absolute .lir path; replaced
        only with replace=true (the replaced file is backed up first), saved and left
        open in LIRA; analyse it with lira_run_analysis and check it with
        lira_slab_deflections. Answer: the copy's path, the elements changed, and how
        many of them had another kE before.
        """
        ref = model or await session.resolve(None)
        chosen = await chosen_elements(ref, elements, edit=True)
        return await session.run(
            ss.cracked_copy,
            session.adapter,
            ref,
            factor,
            chosen,
            save_as,
            replace,
            lambda path: session.backups.ensure(path, fresh=True),
            timeout=session.settings.call_timeout_s * 5,
        )

    @mcp.tool()
    @lira_errors
    @extend_doc(SLABS_HELP)
    async def lira_slab_reinforcement(
        output_dir: str,
        model: str | None = None,
        elements: list[int] | str | None = None,
        components: list[str] | None = None,
        levels: list[float] | None = None,
        background: dict[str, float] | None = None,
        addons: list[dict[str, float]] | None = None,
        formats: list[str] | None = None,
        report: str | None = None,
        title: str | None = None,
        design_option: int = 1,
    ) -> dict[str, Any]:
        """Reinforcement mosaics of slabs: the areas LIRA selected for each slab plate
        (its envelope over the design combinations; per load combination it is not
        available in LIRA 2024), AS1 bottom along X, AS2 top along X, AS3 bottom along
        Y, AS4 top along Y (plate axes), in cm2/m.

        The scale, one for all slabs and components: levels - steps in cm2/m, e.g.
        [0, 2, 4, 6, 8, 10] (bands as LIRA colours its own mosaics; give LIRA's steps
        to compare); or background {"diameter": 12, "spacing": 200} with addons [{...},
        ...] (mm): the background mesh, and per element the smallest add-on that covers
        what it needs, else "more than the largest add-on". Without either: round steps
        over the largest area. Elements LIRA could not design are a band of their own.

        Writes into output_dir (absolute): the report ("both" by default, "docx", "pdf" or
        "none", as lira_slab_deflections: the mosaics of every slab and component with the
        zones' areas) and formats (default ["dxf"]): "dxf" - a DXF per slab
        (<model>_slab<N>_reinforcement.dxf, millimetres) with a plan per component, two in
        a row, the first at the model's coordinates: a layer per component and band
        (AS1_add_D16s200, AS2_4-6, ...) with zone outlines and hatches, the area each element
        needs as text at its centre in a layer per component (AS1_VALUES), the slab outline,
        supports and a legend beside each plan; "png" - the mosaics as files too, only when
        the engineer asks (<model>_slab<N>_AS1.png ...). Answer per slab and component: the
        largest area, its element, and the plan area (m2) of each band.
        """
        comps = components or ["AS1", "AS2", "AS3", "AS4"]
        unknown = sorted(set(comps) - {"AS1", "AS2", "AS3", "AS4"})
        if unknown:
            raise LiraInputError(f"Unknown components {unknown}: AS1, AS2, AS3, AS4")
        formats = formats if formats is not None else ["dxf"]
        if set(formats) - {"png", "dxf"}:
            raise LiraInputError('formats: "png" and / or "dxf"')
        out_dir, report = _checked_output(output_dir, report)
        if out_dir is None:
            raise LiraInputError("output_dir must be an absolute folder path")
        if not (report or formats):
            raise LiraInputError('report "none" and no formats: nothing to write')
        if levels or background or addons:  # a wrong scale is told before a long read
            sm.scale_of(levels, background, addons, {})
        ref = model or await session.resolve(None)
        within = await chosen_elements(ref, elements)
        path, slabs, values, notes = await session.run(
            sm.read_reinforcement,
            session.adapter,
            ref,
            within,
            comps,
            design_option,
            timeout=session.settings.call_timeout_s * 10,
        )
        scale = await asyncio.to_thread(sm.scale_of, levels, background, addons, values)
        found = await asyncio.to_thread(sm.summaries, slabs, values, scale)
        warning = await session.run(session.adapter.results_warning, path)
        files = await asyncio.to_thread(
            sm.mosaic_files, path, slabs, values, scale, found, out_dir, formats, report, title,
            design_option, [n.ru for n in notes] + ([ss.STALE_RESULTS] if warning else []),
            within is not None,
        )  # fmt: skip
        result: dict[str, Any] = {
            "model": path,
            "units": "cm2/m",
            "scale": scale.labels,
            "slabs": found,
            "files": files,
        }
        if notes:
            result["notes"] = [n.text for n in notes]
        if warning:
            result["warning"] = warning
        return result
