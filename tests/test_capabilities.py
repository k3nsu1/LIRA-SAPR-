"""Tools that need an API function this LIRA release lacks say so clearly."""

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.tools.common import REQUIRES, unavailable_tools

pytestmark = pytest.mark.anyio


async def test_a_tool_whose_api_function_is_missing_is_refused_before_it_runs(tmp_path):
    session, adapter = fake_session(tmp_path)
    adapter.capabilities = adapter.capabilities - {"StartAnalysis"}
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_create_model", {"path": str(tmp_path / "m.lir")})
        refused = await client.call_tool("lira_run_analysis", {})
        status = await client.call_tool("lira_status", {})
    assert refused.is_error
    text = refused.content[0].text
    assert "lira_run_analysis needs StartAnalysis" in text and "2024" in text
    assert adapter.analysis == "idle"
    assert status.structured_content["tools_this_lira_cannot_run"] == ["lira_run_analysis"]


async def test_the_first_guarded_call_connects_to_learn_the_capabilities(tmp_path):
    session, adapter = fake_session(tmp_path)
    adapter.capabilities = adapter.capabilities - {"StartAnalysis"}
    assert not adapter.connected
    async with Client(build_server(session)) as client:
        refused = await client.call_tool("lira_run_analysis", {})
        assert adapter.connected  # the session disconnects when the server stops
    assert refused.is_error and "needs StartAnalysis" in refused.content[0].text


async def test_a_full_lira_lists_no_unavailable_tools(tmp_path):
    session, _ = fake_session(tmp_path)
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_list_models", {})  # connects
        status = await client.call_tool("lira_status", {})
    assert "tools_this_lira_cannot_run" not in status.structured_content


def test_every_guarded_tool_names_api_functions_the_real_adapter_probes(tmp_path):
    from lira_sapr_mcp.adapters.com_adapter import APP_METHODS, DOC_METHODS

    build_server(fake_session(tmp_path)[0])
    assert REQUIRES, "no tool is guarded"
    probed = frozenset(APP_METHODS + DOC_METHODS)
    assert {c for caps in REQUIRES.values() for c in caps} <= probed
    assert unavailable_tools(probed) == []
