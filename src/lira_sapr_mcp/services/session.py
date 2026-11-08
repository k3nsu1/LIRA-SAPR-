"""One server session: the chosen LIRA installation, its adapter and the COM worker."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from lira_sapr_mcp.adapters.com_adapter import ComLiraAdapter
from lira_sapr_mcp.adapters.dialogs import Dialog, find_dialogs, press_button
from lira_sapr_mcp.adapters.discovery import LiraInstall, find_installs, pick_install
from lira_sapr_mcp.com.worker import ComWorker
from lira_sapr_mcp.config import Settings
from lira_sapr_mcp.errors import LiraNotInstalledError, LiraTimeoutError
from lira_sapr_mcp.services.backups import Backups

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 180.0


class LiraSession:
    def __init__(
        self,
        settings: Settings,
        *,
        worker: ComWorker | None = None,
        find: Callable[[], list[LiraInstall]] = find_installs,
        make_adapter: Callable[[LiraInstall], Any] = ComLiraAdapter,
        dialogs: Callable[[str], list[Dialog]] = find_dialogs,
        press: Callable[[str, str, str | None], Dialog] = press_button,
    ) -> None:
        self.settings = settings
        self.worker = worker or ComWorker()
        self.backups = Backups(settings.backup_dir_name)
        self._find = find
        self._make_adapter = make_adapter
        self._find_dialogs = dialogs
        self._press = press
        self._installs: list[LiraInstall] | None = None
        self._adapter: Any = None

    @property
    def installs(self) -> list[LiraInstall]:
        if self._installs is None:
            self._installs = self._find()
        return self._installs

    @property
    def install(self) -> LiraInstall:
        chosen = pick_install(self.installs, self.settings.lira_version)
        if chosen is None:
            found = ", ".join(str(i.year) for i in self.installs) or "none"
            wanted = self.settings.lira_version or "2024 or newer"
            raise LiraNotInstalledError(
                f"No supported LIRA-SAPR installation ({wanted}) found; installed: {found}."
            )
        return chosen

    @property
    def adapter(self) -> Any:
        if self._adapter is None:
            self._adapter = self._make_adapter(self.install)
        return self._adapter

    async def run(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None) -> Any:
        """Connect if needed, then run ``fn(*args)`` on the COM thread."""
        adapter = self.adapter
        # Dialogs open before the call may be the user's: a time-out names them apart.
        before = {d.hwnd for d in await self.dialogs()}
        try:
            if not adapter.connected:
                await self.worker.run(adapter.connect, timeout=CONNECT_TIMEOUT_S)
            return await self.worker.run(fn, *args, timeout=timeout or self.settings.call_timeout_s)
        except LiraTimeoutError as exc:
            shown = await self.dialogs()
            if not shown:
                raise
            new = "; ".join(d.describe() for d in shown if d.hwnd not in before)
            old = "; ".join(d.describe() for d in shown if d.hwnd in before)
            message = str(exc)
            if new:
                message += (
                    f" LIRA shows a dialog that opened during this call: {new}. Answer it "
                    "with lira_press_dialog_button, or ask the user to."
                )
            if old:
                message += (
                    f" LIRA shows a dialog that was open before this call, perhaps the "
                    f"user's: {old}. Ask the user to answer it; do not press it yourself."
                )
            raise LiraTimeoutError(message) from None

    async def dialogs(self) -> list[Dialog]:
        """LIRA's open dialogs, found through Win32 off the COM thread (which may be stuck)."""
        try:
            exe = self.install.exe
        except LiraNotInstalledError:
            return []
        try:
            return await asyncio.to_thread(self._find_dialogs, exe)
        except Exception:
            log.exception("Looking for LIRA dialogs failed")
            return []

    async def press_dialog_button(self, button: str, title: str | None) -> Dialog:
        return await asyncio.to_thread(self._press, self.install.exe, button, title)

    async def resolve(self, model: str | None) -> str:
        """Path (or title, for a never-saved model) of ``model``, or of the active model."""
        info = await self.run(self.adapter.document_info, model)
        return info.path or info.title

    def status(self) -> dict[str, Any]:
        installs = [asdict(i) | {"supported": i.supported} for i in self.installs]
        result: dict[str, Any] = {"installations": installs}
        try:
            result["selected"] = self.install.progid
        except LiraNotInstalledError as exc:
            result["selected"] = None
            result["problem"] = str(exc)
        if self._adapter is not None and self._adapter.connected:
            result["connected"] = True
            result["capabilities"] = sorted(self._adapter.capabilities)
            result["input_tables"] = sorted(self._adapter.table_kinds)
        else:
            result["connected"] = False
        result["busy_with"] = self.worker.busy_with
        if self.worker.overdue:
            result["still_running_after_time_out"] = self.worker.overdue
        if self.worker.last_overdue:
            result["last_call_finished_after_time_out"] = self.worker.last_overdue
        return result

    def close(self) -> None:
        if self._adapter is not None and self._adapter.connected:
            try:
                self.worker.call(self._adapter.disconnect, timeout=10)
            except Exception:
                log.warning("could not release LIRA cleanly", exc_info=True)
        self.worker.stop()
