"""End to end through MCP: build frames from text, analyse, read the protocol."""

import pytest
from mcp import Client
from test_live_loads import FRAME

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

UNSTABLE = FRAME.replace("( 5/ 1 1 2 3 4 5 6/ 2 1 2 3 4 5 6/ )\n", "")


async def analyse(client, text, path):
    made = await client.call_tool("lira_import_text_model", {"text": text, "save_as": path})
    assert not made.is_error, made.content
    res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
    assert not res.is_error, res.content
    return res.structured_content


async def test_clean_frame_analysis(mcp, workdir):
    path = str(workdir / "lmcp_an_ok.lir")
    async with Client(mcp) as client:
        status = await analyse(client, FRAME, path)
        protocol = await client.call_tool("lira_analysis_protocol", {})
    assert status["status"] == "finished"
    assert status["protocol"]["clean"], status["protocol"]
    assert "finite element analysis" in status["stages"]
    assert "Расчет успешно завершен" in protocol.structured_content["text"]


async def test_unstable_frame_reports_warnings(mcp, workdir):
    path = str(workdir / "lmcp_an_bad.lir")
    async with Client(mcp) as client:
        status = await analyse(client, UNSTABLE, path)
    assert status["status"] == "finished"
    assert not status["protocol"]["clean"]
    assert any("изменяемая" in p for p in status["protocol"]["problems"])


async def test_unsaved_changes_block_analysis(mcp, workdir):
    path = str(workdir / "lmcp_an_dirty.lir")
    async with Client(mcp) as client:
        await client.call_tool("lira_import_text_model", {"text": FRAME, "save_as": path})
        await client.call_tool(
            "lira_write_table",
            {"table": "Nodes_Coordinates", "rows": [[4, 6, 0, 3.5]], "numbers": [4]},
        )
        res = await client.call_tool("lira_run_analysis", {})
        assert res.is_error and "save first" in res.content[0].text
        await client.call_tool("lira_close_model", {"discard_changes": True})
