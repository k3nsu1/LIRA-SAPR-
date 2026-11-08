"""LIRA's type libraries: the registered API descriptions of each installed release.

A type library lists the interfaces, methods and constants of a COM API. LIRA registers
two: the application's (``LiraSapr.exe``) and the results library's (``LiraResAPI.dll``,
registered as ``LiraSaprRes``). Each release keeps its files in its own folder, so the
results library of a release is the registered one whose file lies in that folder.
"""

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from lira_sapr_mcp.adapters.discovery import LiraInstall

NAME_MARKS = ("lira", "sapr", "sapphire")
RESULTS_ACCESS = "LiraResultsAccess"
# Methods every COM interface inherits: left out of descriptions.
COM_BASE = {"QueryInterface", "AddRef", "Release", "GetTypeInfoCount", "GetTypeInfo",
            "GetIDsOfNames", "Invoke"}  # fmt: skip


@dataclass(frozen=True)
class TypeLib:
    guid: str
    version: str
    name: str
    path: str

    @property
    def major_minor(self) -> tuple[int, int]:
        major, _, minor = self.version.partition(".")
        return int(major, 16), int(minor or "0", 16)


def registered_typelibs() -> list[TypeLib]:
    """Type libraries registered on this computer whose name mentions LIRA."""
    import winreg

    found = []
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib") as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            guid = winreg.EnumKey(root, i)
            try:
                with winreg.OpenKey(root, guid) as key:
                    versions = [winreg.EnumKey(key, j) for j in range(winreg.QueryInfoKey(key)[0])]
                    for version in versions:
                        name = winreg.QueryValue(key, version)
                        if not any(m in name.lower() for m in NAME_MARKS):
                            continue
                        path = _library_file(winreg, key, version)
                        if path:
                            found.append(TypeLib(guid, version, name, path))
            except OSError:
                continue
    return found


def _library_file(winreg, key, version: str) -> str | None:
    for arch in ("win64", "win32"):  # the 64-bit file when both are registered
        try:
            return winreg.QueryValue(key, rf"{version}\0\{arch}")
        except OSError:
            continue
    return None


def install_root(install: LiraInstall) -> Path:
    """The release's own folder: the one that holds ``Bin``."""
    exe = Path(install.exe)
    return next((p.parent for p in exe.parents if p.name.lower() == "bin"), exe.parent)


def results_library(install: LiraInstall, libraries: list[TypeLib]) -> TypeLib | None:
    """The results library of ``install``: registered from that release's folder."""
    root = PureWindowsPath(install_root(install))  # compares case-insensitively
    own = [
        lib
        for lib in libraries
        if "res" in lib.name.lower() and PureWindowsPath(lib.path).is_relative_to(root)
    ]
    return max(own, key=lambda lib: lib.major_minor) if own else None


def coclass_id(path: str, name: str) -> str | None:
    """The CLSID of a creatable class in a type library file."""
    import pythoncom

    tlib = pythoncom.LoadTypeLib(path)
    for i in range(tlib.GetTypeInfoCount()):
        if (
            tlib.GetTypeInfoType(i) == pythoncom.TKIND_COCLASS
            and tlib.GetDocumentation(i)[0] == name
        ):
            return str(tlib.GetTypeInfo(i).GetTypeAttr().iid)
    return None


def describe(path: str) -> list[str]:
    """Every type of a library as text: enums with values, interfaces with methods."""
    import pythoncom

    kinds = {
        pythoncom.TKIND_ENUM: "enum",
        pythoncom.TKIND_INTERFACE: "interface",
        pythoncom.TKIND_DISPATCH: "dispatch",
        pythoncom.TKIND_COCLASS: "coclass",
        pythoncom.TKIND_RECORD: "record",
        pythoncom.TKIND_ALIAS: "alias",
    }
    tlib = pythoncom.LoadTypeLib(path)
    lines = []
    for i in range(tlib.GetTypeInfoCount()):
        info = tlib.GetTypeInfo(i)
        attr = info.GetTypeAttr()
        lines.append(f"{kinds.get(attr.typekind, attr.typekind)} {tlib.GetDocumentation(i)[0]}")
        for k in range(attr.cVars):
            var = info.GetVarDesc(k)
            value = f" = {var.value}" if attr.typekind == pythoncom.TKIND_ENUM else ""
            lines.append(f"    {info.GetNames(var.memid)[0]}{value}")
        seen = set()
        for k in range(attr.cFuncs):
            func = info.GetFuncDesc(k)
            names = info.GetNames(func.memid)
            key = (names[0], func.invkind)
            if key in seen or names[0] in COM_BASE:
                continue
            seen.add(key)
            prefix = {2: "get ", 4: "put ", 8: "putref "}.get(func.invkind, "")
            lines.append(f"    {prefix}{names[0]}({', '.join(names[1:])})")
    return lines
