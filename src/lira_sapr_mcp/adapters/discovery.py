"""Find LIRA installations through their COM registration in the Windows registry.

Each release registers a versioned ProgID: ``LiraSapr.Application.2024`` up to 2024,
``LiraSapphire.Application.<year>`` from 2025 on. The ProgID leads to a CLSID whose
``LocalServer32`` entry is the path of ``LiraSapr.exe``.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

MIN_SUPPORTED_YEAR = 2024
PROGID_FAMILIES = ("LiraSapr", "LiraSapphire")
CANDIDATE_YEARS = range(2020, 2041)


@dataclass(frozen=True)
class LiraInstall:
    year: int
    progid: str
    exe: str
    display_version: str | None = None

    @property
    def supported(self) -> bool:
        return self.year >= MIN_SUPPORTED_YEAR


def candidate_progids() -> list[str]:
    return [f"{fam}.Application.{year}" for year in CANDIDATE_YEARS for fam in PROGID_FAMILIES]


def collect_installs(
    progids: Iterable[str],
    resolve_exe: Callable[[str], str | None],
    display_versions: dict[int, str] | None = None,
) -> list[LiraInstall]:
    """Build the list of installations, newest first, from ProgIDs present in the registry."""
    found: dict[int, LiraInstall] = {}
    for progid in progids:
        m = re.fullmatch(r"(\w+)\.Application\.(\d{4})(?:\.\d+)?", progid)
        if not m:
            continue
        exe = resolve_exe(progid)
        if not exe:
            continue
        year = int(m.group(2))
        dv = (display_versions or {}).get(year)
        # Prefer the newer ProgID family when a release registers both.
        if year not in found or m.group(1) == "LiraSapphire":
            found[year] = LiraInstall(year, progid, exe, dv)
    return sorted(found.values(), key=lambda i: i.year, reverse=True)


def pick_install(installs: list[LiraInstall], year: int | None) -> LiraInstall | None:
    supported = [i for i in installs if i.supported]
    if year is None:
        return supported[0] if supported else None
    return next((i for i in supported if i.year == year), None)


def _read_default(root: int, path: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            return str(value)
    except OSError:
        return None


def _resolve_exe(progid: str) -> str | None:
    import winreg

    clsid = _read_default(winreg.HKEY_CLASSES_ROOT, rf"{progid}\CLSID")
    if not clsid:
        return None
    server = _read_default(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32")
    return server.strip().strip('"') if server else None


def _display_versions() -> dict[int, str]:
    import winreg

    result: dict[int, str] = {}
    roots = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in roots:
        try:
            uninstall = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        with uninstall:
            for i in range(winreg.QueryInfoKey(uninstall)[0]):
                try:
                    with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, i)) as k:
                        name = str(winreg.QueryValueEx(k, "DisplayName")[0])
                        version = str(winreg.QueryValueEx(k, "DisplayVersion")[0])
                except OSError:
                    continue
                m = re.search(r"LIRA[- ](?:SAPR|FEM)\s+(\d{4})", name, re.I)
                if m and "SAPFIR" not in name.upper():
                    year = int(m.group(1))
                    if version > result.get(year, ""):
                        result[year] = version
    return result


def lira_directories(year: int) -> dict[str, str]:
    """LIRA's own folders for one release: DataDirectory, TextDataDirectory, WorkDirectory,
    ResultDirectory (user settings under HKCU)."""
    import winreg

    candidates = [
        rf"Software\LIRA SAPR\LIRA SAPR {year}\Directories",
        rf"Software\LIRA SAPR\LIRA-FEM {year}\Directories",
        rf"Software\LIRA SAPPHIRE\LIRA SAPPHIRE {year}\Directories",
    ]
    for path in candidates:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            continue
        result = {}
        with key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, i)
                if isinstance(value, str) and value:
                    result[name] = value
        return result
    return {}


def find_installs() -> list[LiraInstall]:
    present = [p for p in candidate_progids() if _resolve_exe(p)]
    return collect_installs(present, _resolve_exe, _display_versions())
