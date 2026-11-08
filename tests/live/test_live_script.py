"""The agent's script on a live LIRA: COM objects, tables, constants and the time limit."""

import pytest
from mcp import Client
from test_live_loads import FRAME

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

SCRIPT = """
nodes = adapter.read_table(doc.PathName, "Nodes_Coordinates")
print("title:", doc.Title)
result = {
    "title": doc.Title,
    "nodes": len(nodes),
    "table_code": int(constants.kLiraTable_Nodes_Coordinates),
    "documents": lira.DocumentCount >= 1,
    "selected": doc.Select("1 2", constants.kLiraSel_Els),
}
doc.Unselect("all", constants.kLiraSel_Els)
"""


async def test_script_reads_lira_and_stops_on_time(mcp, workdir):
    path = str(workdir / "lmcp_script.lir")
    async with Client(mcp) as client:
        made = await client.call_tool("lira_import_text_model", {"text": FRAME, "save_as": path})
        assert not made.is_error, made.content
        res = await client.call_tool("lira_run_script", {"code": SCRIPT, "model": path})
        slow = await client.call_tool(
            "lira_run_script", {"code": "while True:\n    doc.Title", "timeout_s": 1}
        )
        after = await client.call_tool("lira_model_summary", {})
        await client.call_tool("lira_close_model", {"model": path, "discard_changes": True})
    answer = res.structured_content
    assert answer["ok"], answer
    assert answer["result"]["title"] == "lmcp_script" and answer["result"]["nodes"] == 4
    assert answer["result"]["table_code"] == 2 and answer["result"]["documents"]
    assert answer["output"] == "title: lmcp_script\n"
    assert (
        not slow.structured_content["ok"] and "longer than 1 s" in slow.structured_content["error"]
    )
    # LIRA keeps working after a stopped script.
    assert after.structured_content["nodes"] == 4
