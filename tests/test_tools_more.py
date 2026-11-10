"""Tools the first tests did not reach: analysis, loads and text, with the fake adapter."""

from pathlib import Path

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.server import build_server

pytestmark = pytest.mark.anyio

FRAME = (
    "( 0/ 1; F/ 2; 5/ )\n( 1/ 10 1 1 2/ )\n( 4/ 0 0 0/ 0 0 3/ )\n( 6/ 1 6 3 1 1/ )\n( 7/ 1 5/ )\n"
)


async def call(client, name, args=None):
    return await client.call_tool(name, args or {})


async def test_analysis_tools_run_report_and_stop(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "frame.lir"
    model.write_bytes(b"lir")
    async with Client(build_server(session)) as client:
        await call(client, "lira_open_model", {"path": str(model)})
        res = await call(client, "lira_run_analysis", {})
        assert res.structured_content["status"] == "finished"
        backups = list((tmp_path / "lira-mcp-backups").glob("frame_*.lir"))
        assert len(backups) == 1
        status = await call(client, "lira_analysis_status", {})
        assert status.structured_content["model"] == str(model)
        protocol = await call(client, "lira_analysis_protocol", {})
        assert "успешно" in protocol.structured_content["text"]
        stopped = await call(client, "lira_stop_analysis", {})
        assert stopped.structured_content["status"] == "stopped"


async def test_analysis_refuses_unsaved_changes(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await call(client, "lira_create_model", {"title": "new"})
        res = await call(client, "lira_run_analysis", {})
    assert res.is_error and "save first" in res.content[0].text


async def test_import_text_backs_up_an_existing_target(tmp_path):
    session, _ = fake_session(tmp_path)
    target = tmp_path / "owner.lir"
    target.write_bytes(b"owner's model")
    async with Client(build_server(session)) as client:
        res = await call(client, "lira_import_text_model", {"text": FRAME, "save_as": str(target)})
        assert not res.is_error, res.content
        backup = res.structured_content["backup"]
        assert backup and Path(backup).read_bytes() == b"owner's model"
        loads = await call(client, "lira_read_loads", {})
        (load,) = loads.structured_content["loads"]
        assert (load["case"], load["target"], load["type"], load["direction"]) == (1, 1, 6, 3)
        assert load["values"] == [5.0]
        text = await call(client, "lira_export_text", {})
        assert "( 4/" in text.structured_content["text"]


async def test_create_with_save_as_backs_up_an_existing_file(tmp_path):
    session, _ = fake_session(tmp_path)
    target = tmp_path / "old.lir"
    target.write_bytes(b"old")
    async with Client(build_server(session)) as client:
        res = await call(client, "lira_create_model", {"title": "x", "save_as": str(target)})
    assert res.structured_content["backup"]


async def test_bad_loads_are_a_clear_error(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "m.lir"
    model.write_bytes(b"x")
    async with Client(build_server(session)) as client:
        await call(client, "lira_open_model", {"path": str(model)})
        res = await call(client, "lira_set_loads", {"loads": [{"case": 1, "target": 2}]})
    assert res.is_error and "each load needs" in res.content[0].text


async def test_list_tools_answer_without_a_model(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        models = await call(client, "lira_list_models", {})
        tables = await call(client, "lira_list_input_tables", {})
    assert not models.is_error and not tables.is_error
    assert "Nodes_Coordinates" in tables.structured_content["result"]


async def test_summary_reports_results_and_delete_removes_them(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "frame.lir"
    model.write_bytes(b"lir")
    async with Client(build_server(session)) as client:
        await call(client, "lira_open_model", {"path": str(model)})
        before = await call(client, "lira_model_summary", {})
        deleted = await call(client, "lira_delete_results", {"model": str(model)})
        again = await call(client, "lira_delete_results", {"model": str(model)})
        after = await call(client, "lira_model_summary", {})
        await call(client, "lira_create_model", {"title": "new"})
        unsaved = await call(client, "lira_model_summary", {})
    results = before.structured_content["results"]
    assert results["present"] and results["written"].startswith("2026")
    # The fake results have 2 nodes and 3 elements; the open model has none.
    assert "another version of the model" in results["note"]
    assert deleted.structured_content["deleted"] is True
    assert again.structured_content["deleted"] is False
    assert after.structured_content["results"] == {"present": False}
    assert unsaved.structured_content["results"]["present"] is False


async def test_punching_check_runs_on_an_analysed_model_given_by_path(tmp_path):
    session, adapter = fake_session(tmp_path)
    model, other = tmp_path / "slab.lir", tmp_path / "other.lir"
    model.write_bytes(b"lir")
    other.write_bytes(b"lir")
    async with Client(build_server(session)) as client:
        await call(client, "lira_open_model", {"path": str(model)})
        await call(client, "lira_run_analysis", {})
        adapter.docs.clear()  # LIRA closes the model after the analysis
        checked = await call(client, "lira_punching_check", {"model": str(model)})
        await call(client, "lira_open_model", {"path": str(other)})
        unanalysed = await call(client, "lira_punching_check", {})
        await call(client, "lira_create_model", {"title": "new"})
        unsaved = await call(client, "lira_punching_check", {})
    assert not checked.is_error, checked.content
    assert checked.structured_content["model"] == str(model)
    assert unanalysed.is_error and "run lira_run_analysis" in unanalysed.content[0].text
    assert unsaved.is_error and "not saved" in unsaved.content[0].text


async def test_delete_results_needs_a_saved_model(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await call(client, "lira_create_model", {"title": "new"})
        res = await call(client, "lira_delete_results", {})
    assert res.is_error and "not saved" in res.content[0].text


async def test_tables_reference_is_a_resource(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        listed = await client.list_resources()
        read = await client.read_resource("lira://reference/tables")
    assert {str(r.uri) for r in listed.resources} == {
        "lira://reference/tables",
        "lira://reference/query",
    }
    text = read.contents[0].text
    assert "Nodes_Coordinates" in text and "universal space bar" in text


@pytest.mark.parametrize("scripts", [True, False])
async def test_query_reference_is_readable_and_not_repeated_in_tools(tmp_path, scripts):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session, scripts=scripts)) as client:
        listed = await client.list_resources()
        read = await client.read_resource("lira://reference/query")
        tools = (await client.list_tools()).tools
    resource = next(r for r in listed.resources if str(r.uri) == "lira://reference/query")
    assert resource.mime_type == "text/markdown"
    assert read.contents[0].mime_type == "text/markdown"
    text = read.contents[0].text
    for detail in (
        "components",
        "sort_by",
        "by_abs=false",
        "default 20",
        "null = up to 500",
        "envelope=true",
        "threshold",
        "export_path",
        "existing file is",
        "replaced",
        "extremes",
        "total_rows",
        '"selected"',
        '"visible"',
        "reopen it first",
    ):
        assert detail in text
    query_tools = {
        "lira_node_displacements",
        "lira_element_forces",
        "lira_combination_displacements",
        "lira_combination_forces",
        "lira_design_forces",
        "lira_reinforcement",
        "lira_fragment_loads",
        "lira_punching_loads",
    }
    linked = {t.name for t in tools if "lira://reference/query" in (t.description or "")}
    assert linked == query_tools
    for tool in tools:
        description = tool.description or ""
        assert "Query arguments (all result tools)" not in description
        if tool.name in query_tools:
            assert description.count("lira://reference/query") == 1
            assert "null = up to 500" not in description


def test_tables_reference_describes_every_table_of_lira_2024():
    import re
    from importlib.resources import files

    typelib = Path(__file__).parents[1] / "docs/research/typelib/LiraSapr-2024R2.txt"
    kinds = set(re.findall(r"kLiraTable_(\w+)", typelib.read_text(encoding="utf-8")))
    kinds -= {"AllTables", "Group", "Cnt"}
    reference = files("lira_sapr_mcp.reference").joinpath("tables.md").read_text("utf-8")
    described = set(re.findall(r"^\| ([^|]+) \|", reference, re.MULTILINE))
    names = {n.strip(" `") for cell in described for n in cell.split(",")}
    assert len(kinds) == 31 and kinds <= names, sorted(kinds - names)


async def test_closing_the_session_releases_lira_and_the_worker(tmp_path):
    session, adapter = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await call(client, "lira_list_models", {})
    assert adapter.connected is False  # the server's lifespan closed the session
    assert session.worker._thread is None


async def test_status_tells_about_a_call_that_finished_after_its_time_out(tmp_path):
    session, _ = fake_session(tmp_path)
    session.worker.last_overdue = {"job": "set_loads", "seconds": 75.0, "ok": True,
                                   "error": None}  # fmt: skip
    async with Client(build_server(session)) as client:
        status = await call(client, "lira_status", {})
    assert status.structured_content["last_call_finished_after_time_out"]["job"] == "set_loads"
