"""``lira-sapr-mcp --diagnose``: a report on the LIRA releases of this computer.

It reads only the Windows registry and LIRA's type library files: LIRA is not started.
An engineer with LIRA 2025 or 2026 sends the report to the developers, who see from it
what the server has to change for that release.
"""

import platform
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from lira_sapr_mcp import __version__
from lira_sapr_mcp.adapters.discovery import (
    LiraInstall,
    find_installs,
    lira_directories,
    pick_install,
)
from lira_sapr_mcp.adapters.typelibs import (
    RESULTS_ACCESS,
    TypeLib,
    coclass_id,
    describe,
    registered_typelibs,
    results_library,
)


def build_report(
    find: Callable[[], list[LiraInstall]] = find_installs,
    typelibs: Callable[[], list[TypeLib]] = registered_typelibs,
    directories: Callable[[int], dict[str, str]] = lira_directories,
    dump: Callable[[str], list[str]] = describe,
    clsid: Callable[[str, str], str | None] = coclass_id,
) -> str:
    lines = [
        f"# lira-sapr-mcp {__version__} diagnose report",
        f"made: {datetime.now():%Y-%m-%d %H:%M}",
        f"windows: {platform.platform()}; python {sys.version.split()[0]}",
        "",
        "## Installations",
    ]
    installs = find()
    chosen = pick_install(installs, None)
    for install in installs:
        mark = " (used by default)" if install == chosen else ""
        support = "" if install.supported else " (not supported: older than 2024)"
        lines.append(
            f"- {install.year}{mark}{support}: {install.progid}, "
            f"version {install.display_version or '?'}, {install.exe}"
        )
    if not installs:
        lines.append("- none found")
    libraries = typelibs()
    for install in installs:
        lines += ["", f"## LIRA {install.year}"]
        for name, folder in sorted(directories(install.year).items()):
            lines.append(f"- folder {name}: {folder}")
        res = results_library(install, libraries)
        if res is None:
            lines.append("- results library: not found in this release's folder")
        else:
            lines.append(
                f"- results library: {res.name} {res.guid} {res.version}, {res.path}, "
                f"{RESULTS_ACCESS} {_safe(lambda r=res: clsid(r.path, RESULTS_ACCESS))}"
            )
    lines += ["", "## Type libraries"]
    for lib in libraries:
        lines.append(f"- {lib.name}: {lib.guid} {lib.version}, {lib.path}")
    for lib in libraries:
        lines += ["", f"## {lib.name} {lib.guid} {lib.version}", "", "```"]
        try:
            lines += dump(lib.path)
        except Exception as exc:  # pywintypes.com_error: an unreadable file
            lines.append(f"could not read: {exc}")
        lines.append("```")
    return "\n".join(lines) + "\n"


def _safe(fn: Callable[[], str | None]) -> str:
    try:
        return fn() or "?"
    except Exception as exc:
        return f"? ({exc})"


def write_report(path: Path | None = None) -> Path:
    """Write the report to ``path``: a file, or a folder for the default file name."""
    name = f"lira-sapr-mcp-diagnose-{datetime.now():%Y%m%d-%H%M}.md"
    target = path or Path.cwd()
    if target.is_dir():
        target = target / name
    target.write_text(build_report(), encoding="utf-8")
    return target
