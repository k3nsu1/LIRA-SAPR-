"""The diagnose report and the choice of each release's results library."""

from lira_sapr_mcp.adapters import results
from lira_sapr_mcp.adapters.discovery import LiraInstall
from lira_sapr_mcp.adapters.typelibs import TypeLib, install_root, results_library
from lira_sapr_mcp.diagnose import build_report

LIRA_2024 = LiraInstall(
    2024,
    "LiraSapr.Application.2024",
    r"C:\Program Files (x86)\LIRA SAPR\LIRA SAPR 2024\Bin\x64\LiraSapr.exe",
    "24.2.3.1",
)
LIRA_2026 = LiraInstall(
    2026,
    "LiraSapphire.Application.2026",
    r"C:\Program Files\LIRA SAPPHIRE\LIRA SAPPHIRE 2026\Bin\x64\LiraSapr.exe",
    "26.1.0.0",
)
RES_2024 = TypeLib(
    "{BD6C09E6-B5EE-4DAB-8D61-C009BA755C46}",
    "1.0",
    "LiraSaprRes",
    r"C:\Program Files (x86)\LIRA SAPR\LIRA SAPR 2024\Bin\x64\LiraResAPI.dll",
)
RES_2026 = TypeLib(
    "{11111111-2222-3333-4444-555555555555}",
    "2.a",
    "LiraSapphireRes",
    r"C:\Program Files\LIRA SAPPHIRE\LIRA SAPPHIRE 2026\Bin\x64\LiraResAPI.dll",
)
APP_2024 = TypeLib(
    "{DC4D6683-AFD3-4E9F-B9DF-10DD5BB38D6C}",
    "1.0",
    "LIRA-FEM 2024",
    r"C:\Program Files (x86)\LIRA SAPR\LIRA SAPR 2024\Bin\x64\LiraSapr.exe",
)


def test_the_release_folder_is_the_one_holding_bin():
    assert str(install_root(LIRA_2024)).endswith("LIRA SAPR 2024")


def test_each_release_gets_the_results_library_from_its_own_folder():
    libraries = [APP_2024, RES_2024, RES_2026]
    assert results_library(LIRA_2024, libraries) == RES_2024
    assert results_library(LIRA_2026, libraries) == RES_2026
    assert RES_2026.major_minor == (2, 10)  # registry versions are hexadecimal


def test_no_results_library_of_its_own_gives_none():
    assert results_library(LIRA_2026, [APP_2024, RES_2024]) is None


def test_a_sibling_folder_with_a_longer_name_is_not_the_release_folder():
    folder = r"C:\Program Files (x86)\LIRA SAPR\LIRA SAPR 2024 R3\Bin"
    sibling = TypeLib(RES_2026.guid, "1.0", "LiraSaprRes", folder + r"\LiraResAPI.dll")
    assert results_library(LIRA_2024, [sibling]) is None
    upper = TypeLib(RES_2024.guid, "1.0", "LiraSaprRes", RES_2024.path.upper())
    assert results_library(LIRA_2024, [upper]) == upper  # Windows paths ignore case


def test_results_reader_uses_the_releases_own_library(monkeypatch):
    monkeypatch.setattr(results, "registered_typelibs", lambda: [RES_2026])
    monkeypatch.setattr(results, "coclass_id", lambda path, name: "{CLASS-2026}")
    chosen = results.ResultsReader(LIRA_2026)._choose_library()
    assert chosen == (RES_2026.guid, 2, 10, "{CLASS-2026}")


def test_results_reader_never_mixes_two_releases(monkeypatch):
    def unreadable(path, name):
        raise OSError("cannot load the type library")

    monkeypatch.setattr(results, "registered_typelibs", lambda: [RES_2026])
    monkeypatch.setattr(results, "coclass_id", unreadable)
    chosen = results.ResultsReader(LIRA_2026)._choose_library()
    assert chosen == (*results.RES_TYPELIB, results.RES_CLASS)  # all of 2024 R2


def test_report_names_installs_folders_libraries_and_their_contents():
    report = build_report(
        find=lambda: [LIRA_2026, LIRA_2024],
        typelibs=lambda: [APP_2024, RES_2024],
        directories=lambda year: {"WorkDirectory": rf"D:\Work{year}"},
        dump=lambda path: ["enum LiraCmdIdEnum", "    kLiraCmdId_FitInWindow = 5085"],
        clsid=lambda path, name: "{4F4546AE-AC54-48A2-9414-1B9EF6519A37}",
    )
    assert "- 2026 (used by default): LiraSapphire.Application.2026" in report
    assert r"folder WorkDirectory: D:\Work2024" in report
    assert "results library: not found in this release's folder" in report  # 2026
    assert "LiraResultsAccess {4F4546AE" in report
    assert report.count("kLiraCmdId_FitInWindow = 5085") == 2  # both libraries dumped


def test_an_unreadable_library_is_reported_not_fatal():
    def broken(path):
        raise OSError("bad file")

    report = build_report(
        find=lambda: [], typelibs=lambda: [RES_2024], dump=broken, clsid=lambda p, n: None
    )
    assert "- none found" in report and "could not read: bad file" in report


def test_cli_writes_the_report(tmp_path, monkeypatch, capsys):
    import sys

    import lira_sapr_mcp.diagnose as diagnose
    from lira_sapr_mcp.__main__ import main

    monkeypatch.setattr(diagnose, "build_report", lambda: "# report\n")
    target = tmp_path / "report.md"
    monkeypatch.setattr(sys, "argv", ["lira-sapr-mcp", "--diagnose", str(target)])
    main()
    assert target.read_text(encoding="utf-8") == "# report\n"
    assert str(target) in capsys.readouterr().out
