"""Selection and fragmentation in LIRA's window, as the user makes them, on example 04."""

import shutil
from pathlib import Path

import pytest
from mcp import Client
from test_live_loads import FRAME

from lira_sapr_mcp.services import loads as loads_service
from lira_sapr_mcp.services.elements import Criteria, find_elements

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

RU = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
FRAME_AND_SLAB = RU / "04_пространственный каркас с фунд_плитой на упругом основании"
FRAGMENT, RESTORE = 5115, 5117  # LiraCmdIdEnum: fragmentation, restore model


def example(workdir: Path) -> Path:
    found = sorted(FRAME_AND_SLAB.glob("*.lir")) if FRAME_AND_SLAB.is_dir() else []
    if not found:
        pytest.skip("LIRA example models are not installed")
    model = workdir / "lmcp_select04.lir"
    shutil.copy(found[0], model)
    return model


def user_selects(adapter, path, elements, nodes=""):
    """What the user does with the mouse: select elements (and nodes) in the window."""
    doc = adapter.find_document(path)
    doc.Unselect("all", 0)
    doc.Unselect("all", 1)
    doc.Select(elements, 0)
    if nodes:
        doc.Select(nodes, 1)


def user_fragments(adapter, path):
    adapter.find_document(path).Activate()
    adapter._app.StartCmd(FRAGMENT)
    adapter._settle(1.5)


def user_restores(adapter, path):
    adapter.find_document(path).Activate()
    adapter._app.StartCmd(RESTORE)
    adapter._settle(1.5)


def test_selected_and_visible_numbers(lira, adapter, workdir):
    path = lira(adapter.open_document, str(example(workdir)))[0].path
    lira(user_selects, adapter, path, "1-10 25", "1-3")
    selected = lira(adapter.selection, path, "selected")
    assert selected == {"elements": [*range(1, 11), 25], "nodes": [1, 2, 3]}
    assert lira(adapter.selection, path, "selected") == selected  # reading keeps it

    everything = lira(adapter.selection, path, "visible")
    assert len(everything["elements"]) == 1924 and len(everything["nodes"]) == 1767

    lira(user_selects, adapter, path, "1-10 25")
    lira(user_fragments, adapter, path)
    shown = lira(adapter.selection, path, "visible")
    lira(user_restores, adapter, path)
    assert shown["elements"] == [*range(1, 11), 25]
    assert 0 < len(shown["nodes"]) < 30


async def test_stiffness_to_the_selected_elements_through_the_server(mcp, lira, adapter, workdir):
    model = example(workdir)
    path = lira(adapter.open_document, str(model))[0].path
    before = {r[0]: r[1] for r in lira(adapter.read_table, path, "Elements_Stiffnesses")}
    # Plates of the slab: the bars of example 04 form structural elements, and LIRA
    # refuses a stiffness for a part of one ("must not differ within a structural element").
    plates = [r[0] for r in lira(adapter.read_table, path, "Elements_TypeAndNodes") if r[1] != 10]
    picked = plates[:3]
    lira(user_selects, adapter, path, " ".join(map(str, picked)))
    async with Client(mcp) as client:
        sel = await client.call_tool("lira_selection", {"model": path})
        copy = await client.call_tool(
            "lira_copy_stiffness", {"model": path, "source": before[picked[0]], "changes": {}}
        )
        target = copy.structured_content["stiffness"]
        done = await client.call_tool(
            "lira_set_stiffness", {"model": path, "elements": "selected", "stiffness": target}
        )
    after = {r[0]: r[1] for r in lira(adapter.read_table, path, "Elements_Stiffnesses")}
    assert sel.structured_content["elements_count"] == 3
    assert not done.is_error, done.content[0].text
    assert done.structured_content["elements"] == 3
    assert [after[e] for e in picked] == [target] * 3
    others = set(before) - set(picked)
    assert {e: after[e] for e in others} == {e: before[e] for e in others}


def test_find_the_slab_by_level_and_within_a_fragment(lira, adapter, workdir):
    path = lira(adapter.open_document, str(example(workdir)))[0].path
    z_of = {r[0]: r[3] for r in lira(adapter.read_table, path, "Nodes_Coordinates")}
    elements = lira(adapter.read_table, path, "Elements_TypeAndNodes")
    plates = {r[0]: [int(n) for n in str(r[2]).replace(",", " ").split()] for r in elements}
    plates = {e: ns for e, ns in plates.items() if len(ns) > 2}
    level = min(z_of[ns[0]] for ns in plates.values())
    flat = sorted(e for e, ns in plates.items() if all(z_of[n] == level for n in ns))

    found = lira(find_elements, adapter, path, Criteria(z=(level, level), fe_types=(41, 42, 44)))
    assert found["elements"] == flat and found["z_range"] == [level, level]

    lira(user_selects, adapter, path, " ".join(map(str, [*flat[:5], 1, 2])))
    lira(user_fragments, adapter, path)
    shown = lira(adapter.selection, path, "visible")["elements"]
    lira(user_restores, adapter, path)
    within = lira(find_elements, adapter, path, Criteria(within=tuple(shown), fe_types=(10,)))
    assert within["elements"] == [1, 2]


async def test_loads_on_the_selected_bar_only_change(mcp, lira, adapter, workdir):
    model = workdir / "lmcp_frame_sel.lir"
    two = FRAME.replace("( 6/ 3 6 3 1 1/ )", "( 6/ 3 6 3 1 1/ 1 6 1 1 1/ 4 0 1 2 2/ )")
    lira(
        loads_service.import_text_model,
        adapter,
        two.replace("( 7/ 1 10/ )", "( 7/ 1 10/ 2 5/ )"),
        str(model),
    )
    lira(user_selects, adapter, str(model), "3")
    async with Client(mcp) as client:
        mine = await client.call_tool(
            "lira_read_loads", {"model": str(model), "elements": "selected"}
        )
        loads = mine.structured_content["loads"]
        assert [(ld["target"], ld["values"][0]) for ld in loads] == [(3, 10.0)]
        assert mine.structured_content["elements"] == "3"
        loads[0]["values"][0] = 12.5
        done = await client.call_tool(
            "lira_set_loads",
            {"model": str(model), "loads": loads, "elements": mine.structured_content["elements"]},
        )
    assert not done.is_error, done.content[0].text
    after = sorted(
        (ld["case"], ld["target"], ld["values"][0])
        for ld in lira(loads_service.read_loads, adapter, str(model))
    )
    assert after == [(1, 1, 10.0), (1, 3, 12.5), (2, 4, 5.0)]


def test_self_weight_gives_the_user_selection_back(lira, adapter, workdir):
    path = lira(adapter.open_document, str(example(workdir)))[0].path
    lira(user_selects, adapter, path, "1-3")
    lira(adapter.self_weight, path, 1, 1.1, [5, 6])
    assert lira(adapter.selection, path, "selected", "elements") == {"elements": [1, 2, 3]}


async def test_results_of_what_the_user_selected(mcp, lira, adapter, workdir):
    path = str(workdir / "lmcp_results_sel.lir")
    async with Client(mcp) as client:
        for name, args in (
            ("lira_import_text_model", {"text": FRAME, "save_as": path}),
            ("lira_run_analysis", {"model": path, "timeout_s": 300}),
            ("lira_open_model", {"path": path}),  # LIRA closes the model to analyse it
        ):
            res = await client.call_tool(name, args)
            assert not res.is_error, (name, res.content)
        lira(user_selects, adapter, path, "1", "3")
        moved = await client.call_tool(
            "lira_node_displacements", {"model": path, "nodes": "selected"}
        )
        forces = await client.call_tool(
            "lira_element_forces", {"model": path, "elements": "selected"}
        )
    assert not moved.is_error and not forces.is_error, (moved.content, forces.content)
    assert {r["node"] for r in moved.structured_content["rows"]} == {3}
    bars = forces.structured_content["families"]["bar"]["rows"]
    assert {r["element"] for r in bars} == {1}
