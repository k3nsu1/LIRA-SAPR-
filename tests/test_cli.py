"""Command-line flags of lira-sapr-mcp, without starting the server or reading LIRA."""

import sys

import pytest

from lira_sapr_mcp import __main__ as cli
from lira_sapr_mcp import diagnose


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["lira-sapr-mcp", *args])
    cli.main()


@pytest.fixture
def report(monkeypatch):
    """Diagnose without the registry: the report text is fixed."""
    monkeypatch.setattr(diagnose, "build_report", lambda: "report\n")


@pytest.mark.parametrize("flag", ["--no-script", "--noscripts", "stdio"])
def test_a_mistyped_flag_stops_instead_of_running_with_scripts(monkeypatch, capsys, flag):
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, flag)
    assert stop.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_help_prints_the_flags(monkeypatch, capsys):
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "--help")
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "--no-scripts" in out and "--diagnose" in out


def test_diagnose_goes_to_the_current_folder_by_default(monkeypatch, tmp_path, report, capsys):
    monkeypatch.chdir(tmp_path)
    run(monkeypatch, "--diagnose", "--no-scripts")  # the next flag is not a file name
    (written,) = tmp_path.glob("lira-sapr-mcp-diagnose-*.md")
    assert written.read_text(encoding="utf-8") == "report\n"
    assert not (tmp_path / "--no-scripts").exists()
    assert str(written) in capsys.readouterr().out


def test_diagnose_into_a_folder_or_a_file(monkeypatch, tmp_path, report):
    run(monkeypatch, "--diagnose", str(tmp_path))
    assert len(list(tmp_path.glob("lira-sapr-mcp-diagnose-*.md"))) == 1
    run(monkeypatch, "--diagnose", str(tmp_path / "mine.md"))
    assert (tmp_path / "mine.md").read_text(encoding="utf-8") == "report\n"


def test_diagnose_into_a_missing_folder_says_so(monkeypatch, tmp_path, report, capsys):
    with pytest.raises(SystemExit) as stop:
        run(monkeypatch, "--diagnose", str(tmp_path / "no" / "such" / "r.md"))
    assert stop.value.code == 2
    assert "Cannot write the report" in capsys.readouterr().err
