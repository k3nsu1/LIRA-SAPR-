import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from lira_sapr_mcp import __version__


def test_version_is_set():
    assert __version__ == "0.2.0"


def test_cli_prints_version():
    out = subprocess.run(
        [sys.executable, "-m", "lira_sapr_mcp", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == f"lira-sapr-mcp {__version__}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_wheel_carries_reference_and_entry_point(tmp_path):
    """What `uvx` installs: the references must ship inside the wheel."""
    root = Path(__file__).parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(root)],
        capture_output=True,
        check=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    info = f"lira_sapr_mcp-{__version__}.dist-info"
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        entry_points = zf.read(f"{info}/entry_points.txt")
        metadata = zf.read(f"{info}/METADATA").decode("utf-8")
    assert "lira_sapr_mcp/reference/tables.md" in names
    assert "lira_sapr_mcp/reference/query.md" in names
    assert f"{info}/licenses/LICENSE" in names and "License-Expression: MIT" in metadata
    assert b"lira-sapr-mcp = lira_sapr_mcp.__main__:main" in entry_points


@pytest.mark.anyio
async def test_tools_page_lists_every_tool(tmp_path):
    """docs/tools.md is the users' list of tools: it must not fall behind the server."""
    from fakes import fake_session
    from lira_sapr_mcp.server import build_server

    page = (Path(__file__).parents[1] / "docs" / "tools.md").read_text(encoding="utf-8")
    async with Client(build_server(fake_session(tmp_path)[0])) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    documented = set(re.findall(r"^\| `(lira_\w+)` \|", page, re.M))
    assert documented == tools
    assert f"{len(tools)} инструмент" in page


@pytest.mark.anyio
@pytest.mark.parametrize("flags", [[], ["--no-scripts"]])
async def test_server_runs_over_stdio(tmp_path, flags):
    """The real entry point, started the way an MCP client starts it."""
    env = dict(os.environ, LIRA_MCP_CONFIG=str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text(f'log_file = "{(tmp_path / "s.log").as_posix()}"\n')
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "lira_sapr_mcp", *flags], env=env
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        status = await client.call_tool("lira_status", {})
    assert {"lira_status", "lira_open_model", "lira_model_summary"} <= tools
    assert ("lira_run_script" in tools) == (not flags)
    assert not status.is_error
    assert (tmp_path / "s.log").read_text(encoding="utf-8").count("starting") == 1
