"""Acceptance example 5: LIRA shows a modal window; the agent sees it and answers it.

LIRA's harmless About box (menu command 57664) stands for any dialog. A call that hangs
behind a dialog is covered by unit tests: COM keeps answering while this box is open.
"""

import anyio
import pytest
from mcp import Client

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

ABOUT = 57664


async def test_agent_sees_and_closes_a_dialog(mcp):
    async with Client(mcp) as client:
        before = await client.call_tool("lira_dialogs", {})
        if before.structured_content["dialogs"]:
            pytest.skip("LIRA already shows a dialog: it may be the user's")
        opened = await client.call_tool("lira_run_script", {"code": f"lira.StartCmd({ABOUT})"})
        assert opened.structured_content["ok"], opened.structured_content
        try:
            # StartCmd returns at once; LIRA opens the window a moment later.
            for _ in range(20):
                shown = (await client.call_tool("lira_dialogs", {})).structured_content
                if shown["dialogs"]:
                    break
                await anyio.sleep(0.25)
            (about,) = shown["dialogs"]
        finally:
            pressed = await client.call_tool("lira_press_dialog_button", {"button": "OK"})
    assert "2024" in about["title"] and "OK" in about["buttons"]
    assert any("2024" in line for line in about["text"])
    assert not pressed.is_error, pressed.content
    assert pressed.structured_content["dialogs"] == []
