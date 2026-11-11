"""Tools: facts of a model for the RPZ and the pressure under footings (SP RK 5.01-102-2013)."""

import asyncio
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.postprocessing import loads_kz as lk
from lira_sapr_mcp.services import check, rpz
from lira_sapr_mcp.services import rpz_pictures as rp
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import lira_errors


def _folder(output_dir: str | None) -> Path | None:
    if output_dir is None:
        return None
    out = Path(output_dir)
    if not out.is_absolute():
        raise LiraInputError("output_dir: an absolute folder path")
    return out


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool()
    @lira_errors
    async def lira_rpz_facts(
        model: str | None = None, output_dir: str | None = None
    ) -> dict[str, Any]:
        """Facts of an analysed model for sections 1.7 and 4 of the explanatory note (RPZ):
        load cases (number, name, kind, and gamma_f and long_term_share as the DCL input
        table holds them - not proof that LIRA applied them: the RSN make-up shows the
        multipliers it did), stiffnesses with their parameters and number of elements, and each load
        combination (RSN) with its make-up: the multiplier of each load case. Neither LIRA's
        API nor DCL gives a load case's coefficient in an RSN; it is fitted from the
        displacements of up to 300 nodes (an RSN's displacements are the sum of the cases'
        with these multipliers, which include 1/γf: for a normative combination 1/γf for
        permanent loads, long-term part / γf for short-term ones). exact=false: the RSN is not
        such a sum (dynamic cases in it, listed in dynamic_cases_left_out) - its make-up is
        approximate - or load cases move the sample nodes proportionally (ambiguous_cases):
        their multipliers are not determined one by one, the make-up is left empty with a
        warning. output_dir (absolute folder): also <model>_rpz_facts.docx with the tables
        and <model>_rpz_facts.json with the same facts and the model's SHA-256 for LIRA+
        (draft exchange format, version 1; "unsaved_changes": true and a warning when the
        model has changes not saved to its file).
        """
        out = _folder(output_dir)
        ref = model or await session.resolve(None)
        facts = await session.run(
            rpz.model_facts, session.adapter, ref, timeout=session.settings.call_timeout_s * 5
        )
        if out is not None:
            unsaved = await session.run(session.adapter.has_unsaved_changes, facts["model"])
            facts["files"] = [
                await asyncio.to_thread(rpz.write_facts, facts, out),
                await asyncio.to_thread(rpz.write_facts_json, facts, out, unsaved),
            ]
            if unsaved:
                facts["warning"] = (
                    "The model may have unsaved changes in LIRA: the SHA-256 in the JSON is of the "
                    "file on disk, not of the model the facts were read from; save it and "
                    "call again"
                )
        return facts

    @mcp.tool()
    @lira_errors
    async def lira_footing_pressure(
        footings: list[dict[str, Any]],
        model: str | None = None,
        load_case: int | None = None,
        combination: int | None = None,
        compression_rz_sign: Literal[-1, 1] | None = None,
    ) -> dict[str, Any]:
        """Pressure under rectangular footings checked by SP RK 5.01-102-2013: mean p <= R
        (4.6.1), edge <= 1.2R and corner <= 1.5R (4.6.19), e <= l/6 with pmin >= 0, and
        pmin/pmax >= 0.25 for crane buildings of 75 t and more, open crane trestles over 15 t,
        tower structures (trapezoid=true) and whenever R <= 150 kPa (4.6.20). p = N/A + γmt·d,
        edge pressures by the linear law (4.6.21); outside the core use compression-only contact.

        The loads come from LIRA's loads on fragment: define a fragment holding the footing
        nodes in LIRA before the analysis. Each footing: {"nodes": [..] (their loads are
        summed), "l": m (along X, the moment RUY turns it), "b": m (along Y, RUX), "d": m (depth
        of the base), "R": kPa (design resistance of the soil, from the engineer), optional
        "gamma_mt" (kN/m3, default 20), "trapezoid", "name"}. Optional center=[global X,Y] in metres
        defaults to the mean of the node coordinates. h_node in metres is their common height
        above the base, default 0 (nodes at the base). Moments at the center include r cross F.
        compression_rz_sign (+1 or -1) is the sign of RZ in compression verified by the engineer.
        Without it pressure is undetermined. A nonpositive resultant with self-weight means uplift.
        Give exactly one of load_case or combination; an RSN is taken as its fitted multipliers
        times the load cases' loads (refused if it is not such a sum). The
        combination for foundations by deformations is the engineer's choice. The engineer must
        verify global force and moment axes and zero rotation of the fragment group about Z.
        """
        if (load_case is None) == (combination is None):
            raise LiraInputError("Give exactly one of load_case or combination")
        if not footings:
            raise LiraInputError("footings: at least one")
        clean = []
        for f in footings:
            try:
                raw_nodes = f["nodes"]
                if not isinstance(raw_nodes, list) or not raw_nodes:
                    raise ValueError("nodes must be a nonempty list of positive node numbers")
                nodes = [int(n) for n in raw_nodes]
                if any(
                    str(n) != str(raw) or n < 1 for n, raw in zip(nodes, raw_nodes, strict=True)
                ):
                    raise ValueError("nodes must be positive integers")
                if len(set(nodes)) != len(nodes):
                    raise ValueError("duplicate nodes in a footing")
                item = {
                    "nodes": nodes,
                    "l": float(f["l"]),
                    "b": float(f["b"]),
                    "d": float(f["d"]),
                    "R": float(f["R"]),
                    "gamma_mt": float(f.get("gamma_mt", 20.0)),
                    "trapezoid": bool(f.get("trapezoid", False)),
                    "name": f.get("name"),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise LiraInputError(f"footing {f}: needs nodes, l, b, d and R ({exc})") from exc
            for key in ("center", "h_node"):
                if key in f:
                    item[key] = f[key]
            clean.append(item)
        ref = model or await session.resolve(None)
        nodes = sorted({n for f in clean for n in f["nodes"]})
        forces = await session.run(
            rpz.reactions,
            session.adapter,
            ref,
            nodes,
            load_case,
            combination,
            timeout=session.settings.call_timeout_s * 5,
        )
        checked = rpz.footing_checks(clean, forces, compression_rz_sign)
        return {
            "source": {"load_case": load_case}
            if combination is None
            else {"combination": combination},
            "axes_requirement": (
                "Engineer must verify global force and moment axes and zero rotation of the "
                "fragment group about Z. Fragment group rotation is not read by this tool."
            ),
            "footings": checked,
            "all_ok": all(f["ok"] for f in checked),
        }

    @mcp.tool()
    @lira_errors
    async def lira_model_check(model: str | None = None) -> dict[str, Any]:
        """Checks of a model before its explanatory note: results present and of this version
        of the model (node and element counts match), not older than the saved model, every
        element with a stiffness (piles FE 57 aside: theirs lives elsewhere), no node outside
        elements, no coincident nodes (within 1 mm), load combinations (RSN) present and every
        load case in at least one of them (by the fitted RSN make-up). all_ok and per check:
        ok, ru (the check in Russian for the engineer), count and up to 20 items found.
        Nothing in the model is changed."""
        ref = model or await session.resolve(None)
        return await session.run(
            check.check_model, session.adapter, ref, timeout=session.settings.call_timeout_s * 5
        )

    @mcp.tool()
    @lira_errors
    async def lira_kz_loads(
        calculations: list[dict[str, Any]],
        city: str | None = None,
        districts: dict[str, str] | None = None,
        output_dir: str | None = None,
        name: str = "объект",
    ) -> dict[str, Any]:
        """Snow and wind loads by the codes of Kazakhstan (characteristic values, no partial
        factor), for section 3 of the RPZ; the same formulas and rounding as the owner's
        «ЛИРА+ Записка» (checked against it number for number). No LIRA model is needed.

        city: one of Алматы, Астана, Жезказган, Караганда, Павлодар, Петропавловск, Темиртау,
        Уральск, Экибастуз - or districts = {"wind": "II", "snow4": "III", "snow5": "III",
        "snow6": "II"} (wind by NTP RK annex Zh; snow by maps 4, 5 and 6) for another place.
        Each calculation: {"kind": ..., parameters}; optional in all: "env" (Ce: "Обычные"
        default, "Не защищенные от ветра", "Закрытые"), "ct" (1).
        - "snow_duopitch" (5.3.3): span, a1, a2 (degrees). Exceptional snow (map 5) for spans of
          18 m and more (NTP RK 3.4.1).
        - "snow_multispan" (5.3.4, annex B.2): span, a1..a4, l2, l3 (m), h (valley depth, m).
        - "snow_step" (5.3.6 and annex B.3): a1 (lower roof), a_up (upper roof), h (height
          difference), b1, b2, b_up (m), optional gamma (snow weight, kN/m3, 2).
        - "wind_building" (chapter 4 with zmin, tables 7.1, 7.2): terrain ("0", "I"-"IV"), h, l1
          (long side), l2 (m), optional roof ("Свес крыши с острыми углами" or "Крыша с
          парапетом (hp/h=0,025|0,05|0,1)"), cdir, cseason, rho.
        - "wind_vault" (7.2.8, figure 7.11): terrain, h (walls to the springing), f (rise), d
          (span), length (m), optional cdir, cseason, rho.
        Answer per calculation: title, values (kg/m², kPa for sk), loads (what, kg/m²) and lines
        (the calculation in Russian). output_dir (absolute folder): <name>_нагрузки.docx with
        all of it and the summary table.
        """
        out = _folder(output_dir)
        if not calculations:
            raise LiraInputError("calculations: at least one")
        try:
            if districts:
                d = {k: districts.get(k, "") for k in ("wind", "snow4", "snow5", "snow6")}
                place = city or lk.PLACE
                c = lk.climate(place, {place: (d["wind"], d["snow4"], d["snow5"], d["snow6"])})
            elif city:
                c = lk.climate(city)
            else:
                raise LiraInputError("Give city or districts")
            results = [_calculation(c, dict(item)) for item in calculations]
        except (ValueError, KeyError, TypeError) as exc:
            raise LiraInputError(f"loads: {exc}") from exc
        answer: dict[str, Any] = {
            "results": [
                {"title": r.title, "values": r.values, "loads": r.loads, "lines": r.lines}
                for r in results
            ]
        }
        if out is not None:
            answer["files"] = [await asyncio.to_thread(rpz.write_loads, results, out, name)]
        return answer

    @mcp.tool()
    @lira_errors
    async def lira_rpz_pictures(output_dir: str, models: list[dict[str, Any]]) -> dict[str, Any]:
        """Pictures for the appendices of the explanatory note (RPZ) as LIRA draws them, laid
        out like the owner's sample RPZ, in output_dir (absolute; the LIRA+ project's
        picture folder): PNG files named "<appendix><nn> <heading> - <what>.png" and
        pictures.json with what each shows. For each picture LIRA's window is set as the
        engineer sets it by hand (results mode, RSN or load cases, an item of LIRA's list,
        a mosaic, a fragment, a projection) and captured; the item LIRA shows is checked.
        LIRA's display settings (scale, loads drawn, values on elements) stay the user's.

        models: [{"model": .lir path or title, "title": "Здание АБК",
        "scheme_appendix": "В" (optional: scheme views isometric, xoy, xoz, or
        "scheme_views"), "appendix": "И" (letter of its results),
        "groups": [{"title": "Расчет балок перекрытия", "stiffnesses": [2, 3, 4] or
        "elements": [...] or "whole_model": true, "view": "isometric" | "xoy" | "xoz",
        "foundation": false, "pictures": [...]}]}]. pictures (optional) items are
        "<mosaic>[:<item>]": mosaic N, Qy, Qz, My, Mz, Mx, disp_X, disp_Y, disp_Z, Rz;
        item MAXIMUM, MINIMUM, MAX ABS (the envelopes), rsn<N>, case<N>, or rsn: the RSN
        with the largest |Z| in the group (said in notes). Steel (LIR-STK, % of the bearing
        capacity; no item, it is LIRA's own envelope), the check of the assigned sections
        or, with steel_sel_, of the sections LIRA selected: per element steel_1LS,
        steel_2LS, steel_LB (local buckling), steel_max (envelope of the three), and
        steel_sel_1LS ... steel_sel_max; by design sections (SP) steel_sec_<check> and
        steel_sel_sec_<check>, check one of normal, shear, reduced (stresses), stab_Y1,
        stab_Z1, stab_2planes, slender_Y1, slender_Z1, slender_ratio, beam_stab, web,
        flange, deflection, ribs, lattice; for these the server sets LIRA's "assigned /
        selected" and "all element types" choices, which stay so. Default for a group:
        N:MAXIMUM, N:MINIMUM, steel_1LS, steel_LB, disp_Z:rsn; with foundation=true:
        Rz:MINIMUM, Rz:MAXIMUM, Rz:rsn, disp_Z:rsn.
        LIRA's window must be visible (not minimized); the user should not work in LIRA
        meanwhile.
        """
        out = _folder(output_dir)
        if out is None:
            raise LiraInputError("output_dir: an absolute folder path")
        pictures = rp.plan(models)
        taken = await session.run(
            rp.take,
            session.adapter,
            pictures,
            timeout=session.settings.call_timeout_s * max(5, len(pictures)),
        )
        files = await asyncio.to_thread(rp.write, taken, out)
        return {
            "files": files,
            "pictures": [
                {"file": p.file, "appendix": p.appendix, "heading": p.heading, "shown": p.shown}
                | ({"notes": p.notes} if p.notes else {})
                for p in taken
            ],
        }


KINDS = {
    "snow_duopitch": (lk.snow_duopitch, ("span", "a1", "a2"), ("env", "ct")),
    "snow_multispan": (
        lk.snow_multispan,
        ("span", "a1", "a2", "a3", "a4", "l2", "l3", "h"),
        ("env", "ct"),
    ),
    "snow_step": (lk.snow_step, ("a1", "a_up", "h", "b1", "b2", "b_up"), ("env", "ct", "gamma")),
    "wind_building": (
        lk.wind_building,
        ("terrain", "h", "l1", "l2"),
        ("roof", "cdir", "cseason", "rho"),
    ),
    "wind_vault": (lk.wind_vault, ("terrain", "h", "f", "d", "length"), ("cdir", "cseason", "rho")),
}


def _calculation(c: lk.Climate, item: dict[str, Any]) -> lk.Result:
    kind = item.pop("kind", None)
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    fn, needed, optional = KINDS[kind]
    missing = [k for k in needed if k not in item]
    unknown = [k for k in item if k not in needed and k not in optional]
    if missing or unknown:
        raise ValueError(
            f"{kind}: "
            + "; ".join(
                ([f"нет {', '.join(missing)}"] if missing else [])
                + ([f"лишние {', '.join(unknown)}"] if unknown else [])
            )
        )
    args = [str(item[k]) if k == "terrain" else float(item[k]) for k in needed]
    kwargs = {
        k: (item[k] if k in ("env", "roof") else float(item[k])) for k in optional if k in item
    }
    return fn(c, *args, **kwargs)
