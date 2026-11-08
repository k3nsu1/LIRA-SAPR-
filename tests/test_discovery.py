from lira_sapr_mcp.adapters.discovery import collect_installs, pick_install

EXES = {
    "LiraSapr.Application.2021": r"C:\L2021\LiraSapr.exe",
    "LiraSapr.Application.2024": r"C:\L2024\LiraSapr.exe",
    "LiraSapphire.Application.2025": r"C:\L2025\LiraSapr.exe",
    "LiraSapr.Application.2026": r"C:\L2026old\LiraSapr.exe",
    "LiraSapphire.Application.2026": r"C:\L2026\LiraSapr.exe",
}


def installs():
    return collect_installs([*EXES, "Garbage.Thing.1"], EXES.get, {2024: "24.2.3.1"})


def test_installs_sorted_newest_first_with_versions():
    found = installs()
    assert [i.year for i in found] == [2026, 2025, 2024, 2021]
    assert found[2].display_version == "24.2.3.1"


def test_newer_progid_family_wins_for_same_year():
    assert installs()[0].progid == "LiraSapphire.Application.2026"


def test_unregistered_progids_are_skipped():
    assert collect_installs(["LiraSapr.Application.2024"], lambda p: None) == []


def test_pick_newest_supported_or_requested_year():
    found = installs()
    assert pick_install(found, None).year == 2026
    assert pick_install(found, 2024).year == 2024
    assert pick_install(found, 2021) is None
    assert pick_install([i for i in found if i.year == 2021], None) is None
