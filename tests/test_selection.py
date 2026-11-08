"""Elements and nodes the user points at in LIRA: "selected" and "visible" in tools."""

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.numbers import format_numbers, parse_numbers
from lira_sapr_mcp.server import build_server

pytestmark = pytest.mark.anyio

NODES = [[n, float(n), 0.0, 0.0] for n in range(1, 7)]
ELEMENTS = [[e, 10, f"{e}, {e + 1}"] for e in range(1, 6)]


async def model_with(client, tmp_path, adapter, **parts):
    model = tmp_path / "frame.lir"
    model.write_bytes(b"lir")
    await client.call_tool("lira_open_model", {"path": str(model)})
    await client.call_tool("lira_write_table", {"table": "Nodes_Coordinates", "rows": NODES})
    rows = {"table": "Elements_TypeAndNodes", "rows": ELEMENTS}
    await client.call_tool("lira_write_table", rows)
    stiffnesses = [[s, 0, "#ffffff", None, "E:3e+006 STD_END"] for s in (1, 2, 3)]
    await client.call_tool("lira_write_table", {"table": "Stiffnesses", "rows": stiffnesses})
    adapter.docs[str(model)].update(parts)
    return model


def test_number_ranges_round_trip():
    assert format_numbers([7, 1, 2, 3, 10, 11, 12, 3]) == "1-3 7 10-12"
    assert format_numbers([]) == ""
    assert parse_numbers(format_numbers([5, 1, 2, 9])) == [1, 2, 5, 9]


async def test_selection_lists_what_the_user_selected_and_what_is_shown(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        selected = {"elements": [1, 2, 3, 5], "nodes": [4]}
        await model_with(client, tmp_path, adapter, selected=selected)
        sel = (await client.call_tool("lira_selection", {})).structured_content
        shown = await client.call_tool("lira_selection", {"part": "visible"})
        bad = await client.call_tool("lira_selection", {"part": "all"})
    assert sel["elements"] == "1-3 5" and sel["elements_count"] == 4
    assert sel["nodes"] == "4" and sel["nodes_count"] == 1
    assert shown.structured_content["elements"] == "1-5"
    assert shown.structured_content["nodes_count"] == 6
    assert bad.is_error and "selected, visible" in bad.content[0].text


async def test_stiffness_goes_to_the_selected_elements_only(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await model_with(client, tmp_path, adapter, selected={"elements": [2, 4], "nodes": []})
        res = await client.call_tool("lira_set_stiffness", {"elements": "selected", "stiffness": 3})
        table = await client.call_tool("lira_read_table", {"table": "Elements_Stiffnesses"})
    assert res.structured_content["elements"] == 2
    assert table.structured_content["rows"] == [[2, 3], [4, 3]]


async def test_nothing_selected_is_refused_before_any_change(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        model = await model_with(client, tmp_path, adapter)
        res = await client.call_tool("lira_set_restraints", {"nodes": "selected", "directions": []})
    assert res.is_error
    assert "No nodes are selected" in res.content[0].text
    assert "Nodes_Restraints" not in adapter.docs[str(model)]["tables"]


async def test_tables_and_results_take_the_visible_part(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        model = await model_with(
            client, tmp_path, adapter, visible={"elements": [3], "nodes": [3, 4]}
        )
        nodes = await client.call_tool(
            "lira_read_table", {"table": "Nodes_Coordinates", "numbers": "visible"}
        )
        moved = await client.call_tool(
            "lira_node_displacements", {"model": str(model), "nodes": "visible"}
        )
        loads = await client.call_tool(
            "lira_read_table", {"table": "LoadCases", "numbers": "visible"}
        )
        forces = await client.call_tool(
            "lira_read_table", {"table": "Bars_InternalForces", "numbers": "visible"}
        )
    assert nodes.structured_content["total_rows"] == 6  # the fake ignores numbers
    assert adapter.asked_nodes == [3, 4]
    assert moved.structured_content["total_rows"] == 2
    assert loads.is_error and "Bad number list" in loads.content[0].text
    assert forces.is_error and "Bad number list" in forces.content[0].text


async def test_selection_needs_the_api_function(tmp_path):
    session, adapter = fake_session(tmp_path)
    adapter.capabilities = adapter.capabilities - {"GetSelection"}
    async with Client(build_server(session)) as client:
        await model_with(client, tmp_path, adapter)
        res = await client.call_tool("lira_set_stiffness", {"elements": "selected", "stiffness": 1})
    assert res.is_error and "needs GetSelection" in res.content[0].text


async def test_appended_help_reaches_the_tool_descriptions(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
    with_query = [
        "lira_node_displacements",
        "lira_element_forces",
        "lira_combination_displacements",
        "lira_combination_forces",
        "lira_design_forces",
        "lira_reinforcement",
        "lira_fragment_loads",
        "lira_punching_loads",
    ]
    for name in with_query:
        assert "Query arguments" in tools[name], name
        assert '"visible"' in tools[name], name
    for name in ("lira_set_stiffness", "lira_set_restraints", "lira_self_weight"):
        assert '"selected"' in tools[name], name


async def test_loads_on_the_selected_elements_only(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        model = await model_with(client, tmp_path, adapter, selected={"elements": [3], "nodes": []})
        adapter.docs[str(model)]["text"] = (
            "( 0/ 1; FRAME/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )\n( 1/ 10 1 1 3/ 10 1 2 4/ 10 1 3 4/ )\n"
            "( 4/ 0 0 0/ 6 0 0/ 0 0 3/ 6 0 3/ )\n( 6/ 3 6 3 1 1/ 2 6 3 1 1/ )\n( 7/ 1 10/ )\n"
        )
        res = await client.call_tool("lira_read_loads", {"elements": "selected"})
    assert [ld["target"] for ld in res.structured_content["loads"]] == [3]


async def test_an_edit_of_everything_visible_without_a_fragment_is_refused(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        model = await model_with(client, tmp_path, adapter)
        whole = await client.call_tool(
            "lira_set_stiffness", {"elements": "visible", "stiffness": 2}
        )
        adapter.docs[str(model)]["visible"] = {"elements": [1, 2], "nodes": [1, 2, 3]}
        part = await client.call_tool("lira_set_stiffness", {"elements": "visible", "stiffness": 2})
        read = await client.call_tool("lira_selection", {"part": "visible"})
    assert whole.is_error and "nothing is fragmented" in whole.content[0].text
    assert part.structured_content["elements"] == 2
    assert "whole" not in read.structured_content


async def test_loads_are_replaced_by_numbers_not_by_the_selection(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await model_with(client, tmp_path, adapter, selected={"elements": [3], "nodes": []})
        load = {"case": 1, "target": 3, "type": 6, "direction": 3, "values": [1]}
        res = await client.call_tool("lira_set_loads", {"loads": [load], "elements": "selected"})
    assert res.is_error and "take them from lira_read_loads" in res.content[0].text
