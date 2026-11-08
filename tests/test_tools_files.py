import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(tmp_path):
    session, adapter = fake_session(tmp_path)
    return build_server(session), adapter


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    return result


async def test_status_does_not_start_lira(server):
    mcp, adapter = server
    async with Client(mcp) as client:
        res = await call(client, "lira_status")
    assert not res.is_error
    assert res.structured_content["selected"] == "LiraSapr.Application.2024"
    assert res.structured_content["connected"] is False
    assert adapter.connected is False


async def test_create_save_and_summary(server, tmp_path):
    mcp, adapter = server
    target = tmp_path / "frame.lir"
    async with Client(mcp) as client:
        res = await call(client, "lira_create_model", title="frame", save_as=str(target))
        assert res.structured_content["model"]["path"] == str(target)
        adapter.docs[str(target)]["tables"]["Nodes_Coordinates"] = [[1, 0, 0, 0], [2, 6, 0, 0]]
        adapter.docs[str(target)]["tables"]["Elements_TypeAndNodes"] = [[1, 10, "1, 2"]]
        summary = (await call(client, "lira_model_summary")).structured_content
    assert summary["nodes"] == 2
    assert summary["elements_by_fe_type"] == {"10": 1}
    assert summary["units"]["forces"] == "kN"


async def test_first_overwrite_is_backed_up_once(server, tmp_path):
    mcp, _ = server
    model = tmp_path / "beam.lir"
    model.write_bytes(b"original")
    async with Client(mcp) as client:
        await call(client, "lira_open_model", path=str(model))
        first = (await call(client, "lira_save_model")).structured_content["backup"]
        second = (await call(client, "lira_save_model")).structured_content["backup"]
    assert first == second
    backups = list((tmp_path / "lira-mcp-backups").glob("beam_*.lir"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"original"


async def test_close_refuses_unsaved_changes(server):
    mcp, adapter = server
    async with Client(mcp) as client:
        await call(client, "lira_create_model", title="draft")
        res = await call(client, "lira_close_model")
        assert res.is_error
        assert "unsaved changes" in res.content[0].text
        res = await call(client, "lira_close_model", discard_changes=True)
        assert not res.is_error
    assert adapter.docs == {}


async def test_lira_errors_are_plain_messages(server):
    mcp, _ = server
    async with Client(mcp) as client:
        res = await call(client, "lira_open_model", path=r"C:\nope\missing.lir")
    assert res.is_error
    # The SDK prefixes "Error executing tool ...:"; no traceback or wrapper text after it.
    assert res.content[0].text.endswith(r": File not found: C:\nope\missing.lir")


async def test_saving_warns_of_scratch_tables_left_in_the_model(server, tmp_path):
    mcp, adapter = server
    model = tmp_path / "beam.lir"
    model.write_bytes(b"original")
    async with Client(mcp) as client:
        await call(client, "lira_open_model", path=str(model))
        clean = (await call(client, "lira_save_model")).structured_content
        adapter.stuck_tables = ["lira-mcp-42-1a2b3c4d"]
        stuck = (await call(client, "lira_save_model")).structured_content
    assert "warning" not in clean
    assert "lira-mcp-42-1a2b3c4d" in stuck["warning"]
