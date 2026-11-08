"""Acceptance example 3: change the stiffness of a group of elements, analyse again and
compare the largest displacements; the original file is backed up next to the model.

On the portal frame of example 1 every bar bends in the frame plane, so doubling the
section width B doubles EF, EI and GF: all displacements halve. Self weight then follows
the new section. The factor kE in Stiffnesses_Factors multiplies the elastic modulus of
its element: kE = 2 on column 1 about halves the shortening of that column.
"""

import shutil
from pathlib import Path

import pytest
from mcp import Client
from test_live_loads import FRAME

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services import loads
from lira_sapr_mcp.services import operations as ops

pytestmark = [pytest.mark.lira, pytest.mark.anyio]


async def ok(client, name, args=None):
    res = await client.call_tool(name, args or {})
    assert not res.is_error, (name, res.content)
    return res.structured_content


async def largest_z(client, path):
    status = await ok(client, "lira_run_analysis", {"model": path, "timeout_s": 300})
    assert status["status"] == "finished", status
    moved = await ok(client, "lira_node_displacements", {"model": path, "sort_by": "Z", "top": 1})
    return abs(moved["rows"][0]["Z"])


async def top_z(client, path):
    """Load case 1 moves of the column tops, nodes 3 and 4 (the frame has no other free
    nodes, so these are only the columns' shortening)."""
    moved = await ok(client, "lira_node_displacements", {"model": path, "nodes": [3, 4]})
    return {r["node"]: abs(r["Z"]) for r in moved["rows"] if r["case"] == 1}


async def test_stiffer_group_moves_less(mcp, workdir):
    path = str(workdir / "lmcp_edit.lir")
    async with Client(mcp) as client:
        await ok(client, "lira_import_text_model", {"text": FRAME, "save_as": path})
        before = await largest_z(client, path)

        await ok(client, "lira_open_model", {"path": path})
        copied = await ok(client, "lira_copy_stiffness", {"source": 1, "changes": {"B": 80}})
        assigned = await ok(
            client, "lira_set_stiffness", {"elements": "1-3", "stiffness": copied["stiffness"]}
        )
        await ok(client, "lira_save_model", {})
        wider = await largest_z(client, path)
        wider_tops = await top_z(client, path)

        await ok(client, "lira_open_model", {"path": path})
        await ok(
            client, "lira_write_table", {"table": "Stiffnesses_Factors", "rows": [[1, 2.0]],
                                         "numbers": [1]}
        )  # fmt: skip
        weight = await ok(client, "lira_self_weight", {"load_case": 2, "safety_factor": 1.1})
        loads = await ok(client, "lira_read_loads", {"load_cases": [2]})
        # Only element 3, though the user had elements 1 and 2 selected in LIRA.
        await ok(client, "lira_run_script", {"code": "doc.Select('1 2', 0)"})
        await ok(client, "lira_self_weight", {"load_case": 3, "elements": [3]})
        only = await ok(client, "lira_read_loads", {"load_cases": [3]})
        await ok(client, "lira_save_model", {})
        await largest_z(client, path)
        stiffer_tops = await top_z(client, path)
    assert copied["stiffness"] == 2
    backup = assigned["backup"]
    assert backup and backup.endswith(".lir") and "lira-mcp-backups" in backup
    assert weight["message"] is None
    # Self weight of the 80 x 60 cm bars: 0.48 m2 x 10.4167 t/m3 x 1.1 = 5.5 t/m, type 16.
    assert {ld["target"] for ld in loads["loads"]} == {1, 2, 3}
    assert all(ld["type"] == 16 for ld in loads["loads"])
    assert loads["loads"][0]["values"][0] == pytest.approx(5.5, abs=0.01)
    assert [(ld["target"], ld["type"]) for ld in only["loads"]] == [(3, 16)]
    assert wider == pytest.approx(before / 2, rel=1e-3)
    # kE = 2 on column 1 about halves its shortening (the beam shifts a little load to
    # the stiffer column); column 2 keeps about its own.
    assert stiffer_tops[3] == pytest.approx(wider_tops[3] / 2, rel=0.03)
    assert stiffer_tops[4] == pytest.approx(wider_tops[4], rel=0.03)


async def test_restore_undoes_a_saved_change(mcp, workdir):
    path = str(workdir / "lmcp_restore.lir")
    async with Client(mcp) as client:
        await ok(client, "lira_import_text_model", {"text": FRAME, "save_as": path})
        added = await ok(client, "lira_add_nodes", {"coordinates": [[3, 0, 6]]})
        await ok(client, "lira_save_model", {})
        changed = await ok(client, "lira_model_summary", {})
        restored = await ok(client, "lira_restore_backup", {"model": path})
        after = await ok(client, "lira_model_summary", {})
        await ok(client, "lira_close_model", {"model": path, "discard_changes": True})
    assert added["backup"] and changed["nodes"] == 5
    assert restored["reopened"] and restored["restored_from"] == added["backup"]
    assert after["nodes"] == 4


async def test_picture_of_the_frame(mcp, workdir):
    import base64
    import struct

    path = str(workdir / "lmcp_picture.lir")
    target = workdir / "frame.png"
    async with Client(mcp) as client:
        await ok(client, "lira_import_text_model", {"text": FRAME, "save_as": path})
        res = await client.call_tool(
            "lira_model_picture", {"view": "xoz", "save_path": str(target)}
        )
        await ok(client, "lira_close_model", {"model": path, "discard_changes": True})
    assert not res.is_error, res.content
    data = base64.b64decode(res.content[0].data)
    assert data == target.read_bytes() and data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    # Cropped to the drawing: a frame 6 m wide and 3 m high is wider than high.
    assert 50 < height < width


async def test_a_text_model_does_not_inherit_factors_of_an_analysed_namesake(mcp, workdir):
    """LIRA's import of a text file named like an analysed model takes that model's kE
    from LIRA's work files; the server imports under a unique name."""
    first, second = workdir / "a" / "lmcp_namesake.lir", workdir / "b" / "lmcp_namesake.lir"
    second.parent.mkdir()
    first.parent.mkdir()
    async with Client(mcp) as client:
        await ok(client, "lira_import_text_model", {"text": FRAME, "save_as": str(first)})
        await ok(client, "lira_write_table", {"table": "Stiffnesses_Factors",
                                               "rows": [[1, 2.0], [2, 2.0], [3, 2.0]]})  # fmt: skip
        await ok(client, "lira_save_model", {})
        await largest_z(client, str(first))
        made = await ok(client, "lira_import_text_model", {"text": FRAME, "save_as": str(second)})
        factors = await ok(client, "lira_read_table", {"table": "Stiffnesses_Factors"})
        await ok(client, "lira_close_model", {"model": str(second), "discard_changes": True})
    assert made["model"]["title"] == "lmcp_namesake"
    assert factors["rows"] == [[1, 1.0], [2, 1.0], [3, 1.0]]


def test_picture_window_is_the_models_even_with_a_longer_name_open(lira, adapter, workdir):
    import win32gui

    short = lira(loads.import_text_model, adapter, FRAME, str(workdir / "lmcp_view.lir")).path
    longer = lira(loads.import_text_model, adapter, FRAME, str(workdir / "lmcp_view2.lir")).path
    try:

        def window_title(ref):
            doc = adapter.find_document(ref)
            doc.Activate()
            return win32gui.GetWindowText(win32gui.GetParent(adapter._view_window(doc)))

        assert lira(window_title, short) == "lmcp_view.lir"
        assert lira(window_title, longer) == "lmcp_view2.lir"
    finally:
        for ref in (short, longer):
            lira(adapter.close_document, ref, True)


RU = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
STEEL = RU / "12_пространственный каркас МК" / "12_3D каркас МК.lir"
PLATE = RU / "02_плита ЖБ" / "02_плита ЖБ.lir"


def stiffnesses(lira, adapter, ref):
    return {r[0]: r for r in lira(adapter.read_table, ref, "Stiffnesses")}


def test_copied_stiffnesses_keep_the_others(lira, adapter, workdir):
    """LIRA drops unused stiffnesses on a write by numbers and refuses to rewrite the
    whole table of a model with steel design materials (example 12)."""
    frame = lira(loads.import_text_model, adapter, FRAME, str(workdir / "lmcp_copies.lir")).path
    first = lira(ops.copy_stiffness, adapter, frame, 1, {"B": 80})
    second = lira(ops.copy_stiffness, adapter, frame, 1, {"H": 90})
    rows = stiffnesses(lira, adapter, frame)
    assert (first, second) == (2, 3) and list(rows) == [1, 2, 3]
    assert "B:80" in rows[2][4] and "H:90" in rows[3][4]
    if not STEEL.exists():
        pytest.skip("LIRA example models are not installed")
    steel = workdir / "lmcp_steel.lir"
    shutil.copy(STEEL, steel)
    ref = lira(adapter.open_document, str(steel))[0].path
    before = stiffnesses(lira, adapter, ref)
    source = min(before)
    added = lira(ops.copy_stiffness, adapter, ref, source, {})
    after = stiffnesses(lira, adapter, ref)
    assert added == max(before) + 1
    assert {n: after[n] for n in before} == before
    assert after[added][1] == before[source][1]  # the same kind of section


def test_new_elements_with_a_rejected_stiffness_are_named(lira, adapter, workdir):
    if not PLATE.exists():
        pytest.skip("LIRA example models are not installed")
    model = workdir / "lmcp_plate.lir"
    shutil.copy(PLATE, model)
    ref = lira(adapter.open_document, str(model))[0].path
    count = len(lira(adapter.read_table, ref, "Elements_TypeAndNodes"))
    with pytest.raises(LiraInputError) as err:  # a bar with the plate's stiffness
        lira(ops.add_elements, adapter, ref, 10, [[1, 2]], 1)
    elements = lira(adapter.read_table, ref, "Elements_TypeAndNodes")
    assert f"Elements {count + 1} were added without a stiffness" in str(err.value)
    assert "lira_set_stiffness" in str(err.value)
    assert elements[-1] == [count + 1, 10, "1, 2"]
