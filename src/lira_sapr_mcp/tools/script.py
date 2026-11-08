"""Tool: run the agent's own Python code with LIRA's COM objects."""

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from lira_sapr_mcp.services import scripts
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import backup_before_change, lira_errors

# The worker waits a little longer than the script's own limit, which stops Python code.
WORKER_MARGIN_S = 15


def register(mcp: MCPServer, session: LiraSession) -> None:
    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=True))
    @lira_errors
    async def lira_run_script(
        code: str, model: str | None = None, timeout_s: float = 120
    ) -> dict[str, Any]:
        """Run Python code inside the server with full access to LIRA, for anything the
        other tools do not cover. Prefer the dedicated tools when one fits.

        Names available: lira (LIRA's Application COM object), doc (the document of
        `model`, default the active one; None if none is open), adapter (the server's
        LIRA adapter: read_table(ref, name), write_table(ref, name, rows), units(),
        node_displacements(path, nodes, cases), element_forces(...), ...), constants
        (LIRA's enum constants, e.g. constants.kLiraTable_Nodes_Coordinates). Assign
        `result` to return a JSON value; print() output is returned as "output".
        The model file is backed up first. Python code stops after timeout_s; a COM call
        that hangs (e.g. LIRA shows a dialog) is not interrupted. LIRA's API description:
        LIRA-FEM COM type library "LiraSapr" (see lira_status capabilities).
        """
        ref = None
        backup = None
        if model is not None or await session.run(session.adapter.list_documents):
            ref = await session.resolve(model)
            backup = await backup_before_change(session, ref)
        answer = await session.run(
            scripts.run_agent_script,
            session.adapter,
            code,
            ref,
            timeout_s,
            timeout=timeout_s + WORKER_MARGIN_S,
        )
        return {"model": ref, "backup": backup, **answer}
