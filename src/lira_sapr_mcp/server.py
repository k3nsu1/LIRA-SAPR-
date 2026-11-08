"""Builds the MCP server: one LIRA session and the tool modules registered on it."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files

from mcp.server import MCPServer

from lira_sapr_mcp import __version__
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools import (
    analysis,
    dialogs,
    editing,
    members,
    results,
    rpz,
    script,
    selection,
    slabs,
    tables,
)
from lira_sapr_mcp.tools import files as file_tools

INSTRUCTIONS = """\
Drives LIRA-SAPR (LIRA-FEM) 2024 R2+ on this computer through its COM API.
Typical flow: lira_status -> lira_open_model or lira_create_model -> lira_model_summary
-> change the model (high-level lira_add_*/lira_set_* tools or input tables)
-> lira_save_model -> run the analysis -> read results.
Calls are executed one at a time by LIRA. Input tables use LIRA's current units, reported
by lira_model_summary. A model argument is a .lir path or a model title; when omitted, the
model active in LIRA is used. Files are backed up before the server first overwrites them.
Results tools return extremes and the top rows only, converted to LIRA's current units.
Table columns, FE type codes and the text format: resource lira://reference/tables.
If a call times out, LIRA may wait in a dialog: see lira_dialogs.
"""
SCRIPT_INSTRUCTIONS = (
    "For anything no tool covers, lira_run_script runs Python with LIRA's COM objects.\n"
)

TABLES_REFERENCE = "lira://reference/tables"


def build_server(session: LiraSession, scripts: bool = True) -> MCPServer:
    """``scripts=False`` (the --no-scripts flag) leaves out lira_run_script."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            session.close()

    instructions = INSTRUCTIONS + (SCRIPT_INSTRUCTIONS if scripts else "")
    mcp = MCPServer("lira-sapr", version=__version__, instructions=instructions, lifespan=lifespan)
    file_tools.register(mcp, session, scripts)
    tables.register(mcp, session)
    editing.register(mcp, session)
    selection.register(mcp, session)
    analysis.register(mcp, session)
    results.register(mcp, session)
    slabs.register(mcp, session)
    members.register(mcp, session)
    rpz.register(mcp, session)
    if scripts:
        script.register(mcp, session)
    dialogs.register(mcp, session)

    @mcp.resource(
        TABLES_REFERENCE,
        title="LIRA input tables, FE types and text format",
        description="Columns of LIRA's input tables, FE type codes, model types and the "
        "text input format, with what is verified and what is only observed.",
        mime_type="text/markdown",
    )
    def tables_reference() -> str:
        return files("lira_sapr_mcp.reference").joinpath("tables.md").read_text(encoding="utf-8")

    return mcp
