"""Server settings: defaults overridden by an optional TOML file.

The file is looked up at ``LIRA_MCP_CONFIG`` or ``%APPDATA%/lira-sapr-mcp/config.toml``.
"""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from lira_sapr_mcp.errors import LiraInputError


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "lira-sapr-mcp"


# --8<-- [start:settings]
@dataclass(frozen=True)
class Settings:
    # LIRA release year to use, e.g. 2024; None picks the newest supported installation.
    lira_version: int | None = None
    # Seconds to wait for one LIRA call before reporting that LIRA is not answering.
    call_timeout_s: float = 60.0
    # Seconds a single analysis may run before the wait gives up (the analysis continues).
    analysis_timeout_s: float = 3600.0
    # Folder next to a model where backups are written before the first change.
    backup_dir_name: str = "lira-mcp-backups"
    log_file: Path = app_data_dir() / "server.log"
    # --8<-- [end:settings]


def default_config_path() -> Path:
    env = os.environ.get("LIRA_MCP_CONFIG")
    return Path(env) if env else app_data_dir() / "config.toml"


# What each setting may be: a check and how to say it in an error.
CHECKS = {
    "lira_version": (lambda v: v is None or (isinstance(v, int) and v >= 2024),
                     "a year such as 2024"),
    "call_timeout_s": (lambda v: isinstance(v, int | float) and v > 0, "seconds above 0"),
    "analysis_timeout_s": (lambda v: isinstance(v, int | float) and v > 0, "seconds above 0"),
    "backup_dir_name": (lambda v: isinstance(v, str) and v.strip(" .") != ""
                        and not set(v) & set(r"\/:"),
                        r"a folder name (not . or ..) without \ / :"),
    "log_file": (lambda v: isinstance(v, str) and v.strip(), "a file path"),
}  # fmt: skip


def load_settings(path: Path | None = None) -> Settings:
    path = path or default_config_path()
    if not path.exists():
        return Settings()
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LiraInputError(f"Cannot read settings file {path}: {exc}") from exc
    known = {f.name for f in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise LiraInputError(f"Unknown settings in {path}: {', '.join(sorted(unknown))}")
    for name, value in raw.items():
        check, wanted = CHECKS[name]
        if isinstance(value, bool) or not check(value):
            raise LiraInputError(f"Setting {name} in {path} must be {wanted}, got {value!r}")
    if "log_file" in raw:
        raw["log_file"] = Path(raw["log_file"])
    return Settings(**raw)
