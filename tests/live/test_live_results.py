"""Acceptance example 1: a portal frame from text, analysed, forces match hand calculation.

Frame 6 x 3 m, fixed bases, equal 40x60 cm bars, 10 t/m on the beam. Slope-deflection
method: beam end moments -23.858 t*m, midspan +21.142 t*m, column base 11.575 t*m,
column axial force -30 t.
"""

import shutil
import time
from pathlib import Path

import pytest
from mcp import Client
from test_live_loads import FRAME

from lira_sapr_mcp.services import results as rs

pytestmark = [pytest.mark.lira, pytest.mark.anyio]


def in_tons(value, unit):
    force, _, per = unit.partition("*")
    return value / (rs.TF_TO[force] * (rs.M_TO[per] if per else 1.0))


async def test_frame_forces_match_hand_calculation(mcp, workdir):
    path = str(workdir / "lmcp_res_frame.lir")
    async with Client(mcp) as client:
        made = await client.call_tool("lira_import_text_model", {"text": FRAME, "save_as": path})
        assert not made.is_error, made.content
        sections = [[1, 3], [2, 3], [3, 3]]
        res = await client.call_tool(
            "lira_write_table", {"table": "Elements_DesignSectionsCount", "rows": sections}
        )
        assert not res.is_error, res.content
        await client.call_tool("lira_save_model", {})
        await client.call_tool("lira_close_model", {})
        await client.call_tool("lira_open_model", {"path": path})
        status = await analyse_open(client, path)
        assert status["protocol"]["clean"], status
        beam = await client.call_tool(
            "lira_element_forces", {"model": path, "elements": "3", "components": ["My"]}
        )
        columns = await client.call_tool(
            "lira_element_forces",
            {"elements": [1, 2], "components": ["N", "My"], "sort_by": "My", "top": None},
        )
        moved = await client.call_tool(
            "lira_node_displacements", {"model": path, "components": ["X", "Z"]}
        )
    for r in (beam, columns, moved):
        assert not r.is_error, r.content
    b = beam.structured_content["families"]["bar"]
    unit = b["units"]["My"]
    my = {r["section"]: in_tons(r["My"], unit) for r in b["rows"]}
    assert my[1] == pytest.approx(-23.858, abs=0.01)
    assert my[2] == pytest.approx(21.142, abs=0.01)
    assert my[3] == pytest.approx(-23.858, abs=0.01)
    c = columns.structured_content["families"]["bar"]
    assert c["total_rows"] == 6
    assert in_tons(c["rows"][0]["My"], c["units"]["My"]) == pytest.approx(23.858, abs=0.01)
    bases = [r for r in c["rows"] if r["section"] == 1]
    for base in bases:
        assert abs(in_tons(base["My"], c["units"]["My"])) == pytest.approx(11.575, abs=0.01)
        assert in_tons(base["N"], c["units"]["N"]) == pytest.approx(-30.0, abs=0.01)
    d = moved.structured_content
    assert d["total_rows"] == 4
    assert d["extremes"]["Z"]["min"]["at"]["node"] in (3, 4)
    assert d["extremes"]["Z"]["min"]["value"] < 0


async def analyse_open(client, path):
    res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
    assert not res.is_error, res.content
    return res.structured_content


async def test_results_of_same_named_and_changed_models_carry_warnings(mcp, workdir):
    """LIRA keeps results by model name only: the server says whose they are."""
    a, b = workdir / "a" / "lmcp_res_twin.lir", workdir / "b" / "lmcp_res_twin.lir"

    async def forces(client, path):
        res = await client.call_tool("lira_element_forces", {"model": str(path), "top": 1})
        assert not res.is_error, res.content
        return res.structured_content

    async with Client(mcp) as client:
        for path in (a, b):
            path.parent.mkdir(exist_ok=True)
            made = await client.call_tool(
                "lira_import_text_model", {"text": FRAME, "save_as": str(path)}
            )
            assert not made.is_error, made.content
            await client.call_tool("lira_close_model", {"model": str(path)})
        await client.call_tool("lira_open_model", {"path": str(a)})
        await analyse_open(client, str(a))
        fresh = await forces(client, a)
        # Only reading the model makes the server save it on closing: still a's results.
        await client.call_tool("lira_open_model", {"path": str(a)})
        await client.call_tool("lira_model_summary", {})
        await client.call_tool("lira_close_model", {"model": str(a)})
        time.sleep(1.5)
        after_read = await forces(client, a)
        await client.call_tool("lira_open_model", {"path": str(b)})
        await analyse_open(client, str(b))
        twin = await forces(client, a)
        await client.call_tool("lira_open_model", {"path": str(b)})
        sections = [[1, 5], [2, 5], [3, 5]]
        await client.call_tool(
            "lira_write_table", {"table": "Elements_DesignSectionsCount", "rows": sections}
        )
        # A save within a second of LIRA's own save at the end of the analysis counts as
        # that save: the stamp cannot tell them apart.
        time.sleep(1.5)
        await client.call_tool("lira_save_model", {})
        await client.call_tool("lira_close_model", {"model": str(b)})
        changed = await forces(client, b)
    assert "warning" not in fresh
    assert "warning" not in after_read
    assert str(b) in twin["warning"] and "same name" in twin["warning"]
    assert "out of date" in changed["warning"]


async def test_results_of_a_model_never_analysed(mcp, workdir):
    path = str(workdir / "lmcp_res_none.lir")
    async with Client(mcp) as client:
        res = await client.call_tool("lira_element_forces", {"model": path})
    assert res.is_error and "No analysis results" in res.content[0].text
    assert "run lira_run_analysis" in res.content[0].text


EXAMPLE_01 = (
    Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
    / "01_плоская рама ЖБ"
    / "01_2D рама ЖБ.lir"
)


async def test_design_forces_and_missing_combinations_of_example_01(mcp, workdir):
    """Frame 01 has design combinations (DCF) but no load combinations (DCL)."""
    if not EXAMPLE_01.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex01.lir")
    shutil.copy(EXAMPLE_01, path)
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        status = await analyse_open(client, path)
        assert status["status"] == "finished", status
        design = await client.call_tool(
            "lira_design_forces", {"model": path, "elements": [1], "limit_states": [1]}
        )
        dcl = await client.call_tool("lira_combination_forces", {"model": path})
        rebar = await client.call_tool("lira_reinforcement", {"model": path, "elements": "1-3"})
        fragment = await client.call_tool("lira_fragment_loads", {"model": path})
        punching = await client.call_tool("lira_punching_loads", {"model": path})
        # LIRA shows nothing when a model has no punching contours: only a timeout tells.
        check = await client.call_tool("lira_punching_check", {"model": path, "timeout_s": 10})
    assert check.is_error and "no punching contours" in check.content[0].text
    assert not rebar.is_error, rebar.content
    bars = rebar.structured_content["families"]["bar"]
    # Only the symmetric variant is selected in this example: AU1 = 2.28 cm2 at element 1.
    assert all(r["symmetric"] for r in bars["rows"])
    first = next(r for r in bars["rows"] if r["element"] == 1 and r["section"] == 1)
    assert first["AU1"] == pytest.approx(2.28, abs=0.01)
    assert fragment.is_error and "define a fragment" in fragment.content[0].text
    assert punching.is_error and "punching shear check" in punching.content[0].text
    assert not design.is_error, design.content
    bar = design.structured_content["families"]["bar"]
    assert bar["total_rows"] >= 5
    first = bar["rows"][0]
    assert first["limit_state"] == "ultimate" and first["group"] in ("A1", "B1", "C1", "D1")
    assert first["load_cases"]
    assert dcl.is_error and "load combinations (input table DCL)" in dcl.content[0].text


EXAMPLE_04 = (
    Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
    / "04_пространственный каркас с фунд_плитой на упругом основании"
    / "04_3D каркас_упругое основание.lir"
)


async def test_plates_and_dynamic_shapes_of_example_04(mcp, workdir):
    """Frame with a slab on elastic base; load case 4 is seismic with modes 1, 5, 11."""
    if not EXAMPLE_04.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex04.lir")
    shutil.copy(EXAMPLE_04, path)
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        status = await analyse_open(client, path)
        assert status["status"] == "finished", status
        forces = await client.call_tool(
            "lira_element_forces",
            {"model": path, "load_cases": [4], "sort_by": "Mx", "top": 5},
        )
        moved = await client.call_tool(
            "lira_node_displacements", {"model": path, "nodes": "100", "load_cases": [4]}
        )
        plate109 = await client.call_tool(
            "lira_element_forces",
            {"model": path, "elements": "109", "load_cases": [4], "top": None},
        )
        listed = await client.call_tool("lira_load_combinations", {"model": path})
        dcl_forces = await client.call_tool(
            "lira_combination_forces",
            {"model": path, "elements": "1", "combinations": [1], "limit_states": [1]},
        )
        dcl_moved = await client.call_tool(
            "lira_combination_displacements", {"model": path, "nodes": "2"}
        )
        periods = await client.call_tool("lira_periods", {"model": path})
        rebar = await client.call_tool("lira_reinforcement", {"model": path, "elements": "109"})
        plates = {"model": path, "families": ["plate"], "sort_by": "Mx", "top": 10}
        started = time.monotonic()
        every = await client.call_tool("lira_element_forces", plates)
        read_s = time.monotonic() - started
        export = workdir / "lmcp_ex04_plates.xlsx"
        env = await client.call_tool(
            "lira_element_forces",
            plates | {"envelope": True, "threshold": 1.0, "export_path": str(export)},
        )
    assert not env.is_error, env.content
    every_plate = every.structured_content["families"]["plate"]
    env_plate = env.structured_content["families"]["plate"]
    # The envelope keeps one row per plate, with the extremes over all cases and shapes.
    assert (
        env_plate["extremes"]["Mx"]["max"]["value"] == every_plate["extremes"]["Mx"]["max"]["value"]
    )
    top = env_plate["rows"][0]
    assert "case" not in top and top["Mx_max_at"]["case"] in (1, 2, 3, 4)
    assert abs(top["Mx"]) >= 1.0 and env_plate["total_rows"] < every_plate["total_rows"]
    assert env.structured_content["export"]["rows"] == env_plate["total_rows"]
    assert export.stat().st_size > 0
    print(f"example 04: {every_plate['total_rows']} plate rows read in {read_s:.1f} s")
    assert not forces.is_error, forces.content
    (seismic,) = periods.structured_content["load_cases"]
    assert seismic["case"] == 4 and len(seismic["modes"]) == 13
    assert seismic["modes"][0]["period"] == pytest.approx(0.4294, abs=1e-3)
    assert seismic["modes"][0]["modal_mass"] == pytest.approx(44.29, abs=0.01)
    plate = rebar.structured_content["families"]["plate"]
    assert plate["rows"][0]["AS1"] == pytest.approx(4.087, abs=0.01)
    combos = listed.structured_content
    assert combos["code"] == "SP" and len(combos["combinations"]) == 3
    assert [c["seismic"] for c in combos["combinations"]] == [False, True, True]
    bar = dcl_forces.structured_content["families"]["bar"]
    assert bar["total_rows"] == 2  # two design sections
    assert dcl_moved.structured_content["total_rows"] == 3
    families = forces.structured_content["families"]
    assert set(families) >= {"bar", "plate"}
    plate = families["plate"]
    assert {"Mx", "My", "Qx"} <= set(plate["units"])
    assert {r["shape"] for r in plate["rows"]} <= {1, 5, 11, "sum"}
    assert plate["extremes"]["Mx"]["max"]["at"]["case"] == 4
    d = moved.structured_content
    by_shape = {r["shape"]: r for r in d["rows"]}
    assert set(by_shape) == {1, 5, 11, "sum"}
    # LIRA's summary: the square root of the sum of squares of the shapes, with a sign.
    for c in ("X", "Z"):
        srss = sum(by_shape[s][c] ** 2 for s in (1, 5, 11)) ** 0.5
        assert abs(by_shape["sum"][c]) == pytest.approx(srss, rel=1e-5)
    rows = plate109.structured_content["families"]["plate"]["rows"]
    mx = {r["shape"]: r["Mx"] for r in rows}
    assert set(mx) == {1, 5, 11, "sum"}
    srss = sum(mx[s] ** 2 for s in (1, 5, 11)) ** 0.5
    assert abs(mx["sum"]) == pytest.approx(srss, rel=1e-5)


FRAGMENT_MODEL = Path(r"D:\Lira_model\LIRA SAPR 2024\Work\test\test_1.lir")
CSV_LOADS_ON_FRAGMENT = 26  # LiraCsvTableEnum


async def test_loads_on_fragment_and_punching_contour_of_the_owners_model(mcp, workdir):
    """The owner's model with a fragment of 5 nodes and a punching contour; load case 11
    is seismic with modes 13, 15-18, 25. LIRA writes its own table of loads on fragment
    (CSV) during the analysis; the tool must give the same numbers, and the summary
    shape must be the square root of the sum of squares of the shapes."""
    if not FRAGMENT_MODEL.exists():
        pytest.skip("the owner's model with a fragment is not on this computer")
    path = str(workdir / "lmcp_frag.lir")
    shutil.copy(FRAGMENT_MODEL, path)
    csv_on = (
        f"doc.SetGenerateCsvTables(True, [{CSV_LOADS_ON_FRAGMENT}])\n"
        "doc.Save(False)\n"
        "result = adapter.directories()['WorkDirectory']"
    )
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        script = await client.call_tool("lira_run_script", {"code": csv_on, "model": path})
        assert not script.is_error, script.content
        status = await analyse_open(client, path)
        assert status["status"] == "finished", status
        fragment = await client.call_tool("lira_fragment_loads", {"model": path, "top": None})
        no_punching = await client.call_tool("lira_punching_loads", {"model": path})
        check = await client.call_tool("lira_punching_check", {"model": path})
        punching = await client.call_tool("lira_punching_loads", {"model": path, "top": None})
        column = await client.call_tool(
            "lira_element_forces", {"model": path, "elements": "13", "top": None}
        )
        listed = await client.call_tool("lira_list_models", {})
        # LIRA's analysis keeps the loads of the earlier check: the server refuses them.
        await client.call_tool("lira_open_model", {"path": path})
        again = await analyse_open(client, path)
        assert again["status"] == "finished", again
        outdated = await client.call_tool("lira_punching_loads", {"model": path})
    assert outdated.is_error and "before the last analysis" in outdated.content[0].text
    assert not fragment.is_error, fragment.content
    got = {(r["node"], r["case"], r.get("shape")): r for r in fragment.structured_content["rows"]}
    work = Path(script.structured_content["result"])
    (table,) = (work / "lmcp_frag.DOCS" / "AUTORES").glob(f"*.{CSV_LOADS_ON_FRAGMENT}_1.full.csv")
    lines = table.read_text(encoding="utf-8-sig").splitlines()
    header = lines[0].split(";")
    components = ["RX", "RY", "RZ", "RUX", "RUY", "RUZ"]
    checked, wrong = 0, []
    for line in lines[1:]:
        cells = line.split(";")
        if not cells[1].isdigit():
            continue  # a load case title row
        case, _, mode = cells[2].partition(" - ")  # "11 - 13": load case 11, mode 13
        key = (int(cells[header.index("УЗЕЛ")]), int(case), int(mode) if mode else None)
        for c in components:
            lira = float(cells[header.index(c)])
            # LIRA's table prints 6 significant digits and leaves out tiny values as 0.
            if got[key][c] != pytest.approx(lira, rel=1e-4, abs=2e-3):
                wrong.append((key, c, got[key][c], lira))
        checked += 1
    assert not wrong
    assert checked == 5 * 16  # 5 nodes; 10 static load cases and 6 seismic modes
    seismic = {r["shape"]: r for r in got.values() if r["node"] == 6957 and r["case"] == 11}
    assert set(seismic) == {13, 15, 16, 17, 18, 25, "sum"}
    for c in components:
        srss = sum(seismic[s][c] ** 2 for s in (13, 15, 16, 17, 18, 25)) ** 0.5
        assert abs(seismic["sum"][c]) == pytest.approx(srss, rel=1e-5)
    # LIRA computes loads on punching contours only in its punching shear check. The
    # model has one contour, around node 315, where column 13 ends: the contour takes
    # the column's end forces (section 3).
    assert no_punching.is_error and "lira_punching_check" in no_punching.content[0].text
    assert not check.is_error, check.content
    assert not punching.is_error, punching.content
    assert "warning" not in punching.structured_content  # the results stay the model's own
    (doc,) = [d for d in listed.structured_content["result"] if Path(d["path"]) == Path(path)]
    assert not doc["modified"]
    rows = punching.structured_content["rows"]
    assert {r["node"] for r in rows} == {315}
    bar = column.structured_content["families"]["bar"]["rows"]
    end = {(r["case"], r.get("shape")): r for r in bar if r["section"] == 3}
    for r in rows:
        if r["case"] == 11:
            continue  # seismic shapes: checked below
        forces = end[(r["case"], None)]
        assert r["N"] == pytest.approx(forces["N"], rel=1e-4, abs=1e-3)
        assert r["MX"] == pytest.approx(-forces["Mz"], rel=1e-4, abs=1e-3)  # other axes
        assert r["MY"] == pytest.approx(forces["My"], rel=1e-4, abs=1e-3)
    seismic = {r["shape"]: r for r in rows if r["case"] == 11}
    assert set(seismic) == {13, 15, 16, 17, 18, 25, "sum"}
    for c in ("N", "MX", "MY"):
        srss = sum(seismic[s][c] ** 2 for s in (13, 15, 16, 17, 18, 25)) ** 0.5
        assert abs(seismic["sum"][c]) == pytest.approx(srss, rel=1e-5)


EXAMPLE_02 = (
    Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
    / "02_плита ЖБ"
    / "02_плита ЖБ.lir"
)


async def test_summary_and_results_of_example_02(mcp, workdir):
    """Acceptance example 2: the summary of a ready model, as LIRA shows it: a slab of
    91 nodes, 72 plate elements (FE type 11) and 3 load cases."""
    if not EXAMPLE_02.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex02.lir")
    shutil.copy(EXAMPLE_02, path)
    async with Client(mcp) as client:
        await client.call_tool("lira_delete_results", {"model": path})
        await client.call_tool("lira_open_model", {"path": path})
        before = await client.call_tool("lira_model_summary", {})
        status = await analyse_open(client, path)
        assert status["status"] == "finished", status
        await client.call_tool("lira_open_model", {"path": path})
        after = await client.call_tool("lira_model_summary", {})
        await client.call_tool("lira_close_model", {"model": path, "discard_changes": True})
        deleted = await client.call_tool("lira_delete_results", {"model": path})
        forces = await client.call_tool("lira_element_forces", {"model": path})
    s = before.structured_content
    assert (s["nodes"], s["elements"], len(s["load_cases"])) == (91, 72, 3)
    assert s["elements_by_fe_type"] == {"11": 72}
    assert s["results"] == {"present": False}
    results = after.structured_content["results"]
    assert results["present"] and (results["nodes"], results["elements"]) == (91, 72)
    assert "note" not in results and results["written"]
    assert deleted.structured_content["deleted"] is True
    assert forces.is_error and "No analysis results" in forces.content[0].text
