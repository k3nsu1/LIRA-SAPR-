import pytest

from lira_sapr_mcp.config import Settings, load_settings
from lira_sapr_mcp.errors import LiraInputError


def test_missing_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "none.toml") == Settings()


def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('lira_version = 2024\ncall_timeout_s = 5\nlog_file = "x.log"\n')
    s = load_settings(p)
    assert s.lira_version == 2024
    assert s.call_timeout_s == 5
    assert s.log_file.name == "x.log"


def test_unknown_keys_are_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("call_timeout = 5\n")
    with pytest.raises(LiraInputError, match="call_timeout"):
        load_settings(p)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("call_timeout_s = -1", "call_timeout_s .* must be seconds above 0"),
        ('call_timeout_s = "60"', "must be seconds above 0"),
        ("lira_version = 2021", "must be a year such as 2024"),
        ("lira_version = true", "must be a year"),
        ('backup_dir_name = "a/b"', "must be a folder name"),
        ('backup_dir_name = ".."', "must be a folder name"),
        ("call_timeout_s = ", "Cannot read settings file"),
    ],
)
def test_wrong_values_are_named(tmp_path, text, message):
    p = tmp_path / "config.toml"
    p.write_text(text + "\n")
    with pytest.raises(LiraInputError, match=message):
        load_settings(p)


def test_a_bad_settings_file_stops_the_server_with_a_message(tmp_path):
    import os
    import subprocess
    import sys

    (tmp_path / "config.toml").write_text("call_timeout_s = 0\n")
    env = dict(os.environ, LIRA_MCP_CONFIG=str(tmp_path / "config.toml"), APPDATA=str(tmp_path))
    out = subprocess.run(
        [sys.executable, "-m", "lira_sapr_mcp"], capture_output=True, text=True, env=env
    )
    assert out.returncode == 2
    assert "call_timeout_s" in out.stderr and out.stdout == ""
    log = (tmp_path / "lira-sapr-mcp" / "server.log").read_text(encoding="utf-8")
    assert "call_timeout_s" in log
