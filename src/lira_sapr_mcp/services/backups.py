"""Copies of model files made before the server changes or overwrites them."""

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraInputError

log = logging.getLogger(__name__)


class Backups:
    """Back up each model file once per server session, before its first change."""

    def __init__(self, dir_name: str) -> None:
        self.dir_name = dir_name
        self._done: dict[str, Path] = {}

    def ensure(self, model_path: str, fresh: bool = False) -> Path | None:
        """Copy ``model_path`` into the backup folder next to it, unless done already in
        this session (always when ``fresh``).

        Returns the backup path, or None when there is no file on disk yet.
        """
        src = Path(model_path)
        key = str(src.resolve()).lower()
        if key in self._done and not fresh:
            return self._done[key]
        if src.suffix.lower() != ".lir" or not src.is_file():
            return None
        dst = self.snapshot(src)
        self._done[key] = dst
        return dst

    def snapshot(self, src: Path) -> Path:
        """A new backup of ``src`` now, whatever was backed up before."""
        folder = src.parent / self.dir_name
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = folder / f"{src.stem}_{stamp}.lir"
        n = 1
        while dst.exists():  # two backups within one second
            n += 1
            dst = folder / f"{src.stem}_{stamp}-{n}.lir"
        try:
            folder.mkdir(exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            raise LiraInputError(
                f"Could not back up {src} to {folder}: {exc}. Nothing was changed."
            ) from exc
        log.info("backup of %s written to %s", src, dst)
        return dst

    def of(self, model_path: str) -> list[dict[str, Any]]:
        """Backups of a model, newest first."""
        src = Path(model_path)
        folder = src.parent / self.dir_name
        mine = re.compile(rf"^{re.escape(src.stem)}_(\d{{8}}-\d{{6}})(-\d+)?\.lir$", re.I)
        found = []
        for f in folder.glob("*.lir") if folder.is_dir() else []:
            match = mine.match(f.name)
            if match:
                stamp, extra = match.group(1), int((match.group(2) or "-1")[1:])
                made = datetime.strptime(stamp, "%Y%m%d-%H%M%S").isoformat()
                found.append(((stamp, extra), {"path": str(f), "made": made,
                                               "bytes": f.stat().st_size}))  # fmt: skip
        return [b for _, b in sorted(found, key=lambda x: x[0], reverse=True)]

    def choose(self, model_path: str, backup: str | None = None) -> Path:
        """The backup to restore: ``backup`` if it is one of the model's, else the newest."""
        target = Path(model_path)
        backups = self.of(model_path)
        if backup is None:
            if not backups:
                raise LiraInputError(f"No backups of {target} in {target.parent / self.dir_name}")
            return Path(backups[0]["path"])
        if str(Path(backup)).lower() not in {b["path"].lower() for b in backups}:
            raise LiraInputError(f"{backup} is not a backup of {target}: see lira_list_backups")
        return Path(backup)

    def restore(self, model_path: str, chosen: Path) -> dict[str, str]:
        """Copy a backup (from ``choose``) over the model file, after a backup of the file
        as it is now, so that the restore itself can be undone. The model must not be
        open in LIRA."""
        target = Path(model_path)
        before = self.snapshot(target) if target.is_file() else None
        try:
            shutil.copy2(chosen, target)
        except OSError as exc:
            raise LiraInputError(f"Could not restore {chosen} to {target}: {exc}") from exc
        log.info("restored %s from %s", target, chosen)
        return {"restored_from": str(chosen), "previous_version": str(before) if before else ""}
