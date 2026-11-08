"""End to end: an MCP client talks to the server, which drives the real LIRA."""

import pytest
from mcp import Client

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

EXAMPLE = "LIRA SAPR 2024/Manual/RU/01_плоская рама ЖБ/01_2D рама ЖБ.lir"


async def test_open_example_and_summarise(mcp, workdir):
    import shutil
    from pathlib import Path

    src = Path(r"C:\Users\Public\Documents\LIRA SAPR") / EXAMPLE
    if not src.exists():
        pytest.skip("LIRA example models are not installed")
    model = workdir / "lmcp_example01.lir"
    shutil.copy(src, model)
    async with Client(mcp) as client:
        opened = await client.call_tool("lira_open_model", {"path": str(model)})
        assert not opened.is_error, opened.content
        summary = (await client.call_tool("lira_model_summary", {})).structured_content
        assert summary["nodes"] == 9
        assert summary["elements"] == 10
        assert [lc["number"] for lc in summary["load_cases"]] == [1, 2, 3, 4]
        saved = await client.call_tool("lira_save_model", {})
        assert saved.structured_content["backup"]
        closed = await client.call_tool("lira_close_model", {})
        assert not closed.is_error, closed.content
