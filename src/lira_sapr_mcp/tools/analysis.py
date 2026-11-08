"""Tools: run, watch and stop the LIRA analysis; read its protocol."""

import asyncio
import time
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services.session import LiraSession
from lira_sapr_mcp.tools.common import READ_ONLY, lira_errors, needs

POLL_S = 0.5
PUNCHING_TIMEOUT_S = 120.0


def register(mcp: MCPServer, session: LiraSession) -> None:
    async def wait_for_end(timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            status = await session.run(session.adapter.analysis_status)
            if status["status"] == "needs_user":
                status["dialogs"] = [d.describe() for d in await session.dialogs()]
            if status["status"] not in ("running",) or time.monotonic() > deadline:
                return status
            await asyncio.sleep(POLL_S)

    @mcp.tool()
    @lira_errors
    @needs(session, "StartAnalysis")
    async def lira_run_analysis(
        model: str | None = None, wait: bool = True, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """Analyse a saved model (default: the active one).

        LIRA analyses the .lir file on disk, so save changes first (lira_save_model).
        With wait=true the call returns when the analysis ends or timeout_s passes (the
        analysis then keeps running: poll lira_analysis_status). The result includes the
        protocol verdict and every warning line (e.g. a geometrically unstable structure
        still "finishes", with warnings). Status "needs_user" means a LIRA dialog waits.
        LIRA closes the model after the analysis: reopen it to continue editing.
        """
        ref = await session.resolve(model)
        # LIRA analyses the file; a model we only read may be saved first (see adapter).
        session.backups.ensure(ref)
        status = await session.run(session.adapter.start_analysis, ref)
        if not wait:
            return status
        return await wait_for_end(timeout_s or session.settings.analysis_timeout_s)

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_analysis_status(wait_s: float = 0) -> dict[str, Any]:
        """State of the last analysis started by this server: running, finished, failed,
        stopped or needs_user; with the protocol summary when it has ended. wait_s > 0
        waits up to that many seconds for a running analysis to end.
        """
        if wait_s > 0:
            return await wait_for_end(wait_s)
        status = await session.run(session.adapter.analysis_status)
        if status["status"] == "needs_user":
            status["dialogs"] = [d.describe() for d in await session.dialogs()]
        return status

    @mcp.tool()
    @lira_errors
    @needs(session, "StopAnalysis")
    async def lira_stop_analysis() -> dict[str, Any]:
        """Ask LIRA to stop the running analysis."""
        await session.run(session.adapter.stop_analysis)
        return await wait_for_end(30)

    @mcp.tool()
    @lira_errors
    @needs(session, "StartCmd")
    async def lira_punching_check(
        model: str | None = None, timeout_s: float = PUNCHING_TIMEOUT_S
    ) -> dict[str, Any]:
        """Run LIRA's punching shear check of an analysed model (a .lir path, which may be
        closed, or an open model; default: the active one), as LIRA's menu "Расчет арматуры
        > Расчет продавливания" does. For the punching contours defined in the model LIRA
        computes the loads on the contours (read them with lira_punching_loads) and the
        punching reinforcement. An analysis does not run this check, and loads from a check
        run before the last analysis count as outdated: run it after each
        lira_run_analysis. The model is opened if
        LIRA closed it after the analysis; the model file is not changed.
        """
        path = model if model and model.lower().endswith(".lir") else None
        path = path or await session.resolve(model)
        if not path.lower().endswith(".lir"):
            raise LiraInputError(f"{path} is not saved, so it has no analysis results")
        await session.run(session.adapter.open_document, path)
        return await session.run(
            session.adapter.punching_check, path, timeout_s, timeout=timeout_s + 30
        )

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True))
    @lira_errors
    @needs(session, "DeleteResults")
    async def lira_delete_results(model: str | None = None) -> dict[str, Any]:
        """Delete the analysis results of a model (a .lir path, which may be closed, or an
        open model; default: the active one) from LIRA's work folder. The model file is
        not changed; lira_run_analysis creates the results again. LIRA keeps results by
        model name, so this also deletes results of a same-named model from another folder.
        """
        path = model if model and model.lower().endswith(".lir") else None
        path = path or await session.resolve(model)
        if not path.lower().endswith(".lir"):
            raise LiraInputError(f"{path} is not saved, so it has no results")
        before = await session.run(session.adapter.results_info, path)
        if before["present"]:
            await session.run(session.adapter.delete_results, path)
        return {"model": path, "deleted": before["present"], "results_before": before}

    @mcp.tool(annotations=READ_ONLY)
    @lira_errors
    async def lira_analysis_protocol(
        model: str | None = None, tail_lines: int = 200
    ) -> dict[str, Any]:
        """The solver's protocol of the last analysis of a model (default: the active one,
        or the model analysed last), last tail_lines lines.
        """
        if model is None:
            last = (await session.run(session.adapter.analysis_status))["model"]
            model = last or await session.resolve(None)
        text = await session.run(session.adapter.protocol, model)
        if text is None:
            raise LiraInputError(f"No analysis protocol found for {model}")
        lines = text.splitlines()
        return {"model": model, "lines": len(lines), "text": "\n".join(lines[-tail_lines:])}
