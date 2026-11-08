"""Tools: see and answer LIRA's modal dialogs (through Win32, even when COM is stuck)."""

import asyncio
from typing import Any

from mcp.server import MCPServer

from lira_sapr_mcp.adapters.dialogs import Dialog
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import READ_ONLY, lira_errors

# Time for LIRA to close the dialog after a click and maybe show the next one.
AFTER_CLICK_S = 0.7


def shown(dialogs: list[Dialog]) -> list[dict[str, Any]]:
    return [{"title": d.title, "text": d.text, "buttons": d.buttons} for d in dialogs]


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_dialogs() -> dict[str, Any]:
        """Modal dialogs LIRA shows now: title, text and buttons. A call that timed out or
        an analysis with status needs_user usually waits for one of them."""
        return {"dialogs": shown(await session.dialogs())}

    @mcp.tool()
    @lira_errors
    async def lira_press_dialog_button(button: str, title: str | None = None) -> dict[str, Any]:
        """Press a button of LIRA's dialog by its text (e.g. "OK", "Да", "Отмена"; case
        and '&' ignored). title: part of the dialog's title, needed when LIRA shows
        several. Press only dialogs your own calls caused; the user may be answering
        others. Returns the dialogs still shown.
        """
        pressed = await session.press_dialog_button(button, title)
        await asyncio.sleep(AFTER_CLICK_S)
        return {"pressed": {"title": pressed.title, "button": button},
                "dialogs": shown(await session.dialogs())}  # fmt: skip
