"""The agent's script: result, output, errors, time limit, and the tool around it."""

import sys

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.scripts import MAX_TEXT, run_script


def test_result_and_output_come_back():
    code = "print('n =', n)\nresult = {'double': n * 2}"
    answer = run_script(code, {"n": 21}, timeout_s=5)
    assert answer["ok"] and answer["result"] == {"double": 42}
    assert answer["output"] == "n = 21\n"


def test_errors_show_the_script_line_only():
    answer = run_script("x = 1\n\ndef f():\n    return 1 / 0\nf()", {}, timeout_s=5)
    assert not answer["ok"]
    assert "line 5, in <module>" in answer["error"] and "line 4, in f" in answer["error"]
    assert "ZeroDivisionError" in answer["error"] and "scripts.py" not in answer["error"]


def test_syntax_errors_and_exit_do_not_escape():
    assert "SyntaxError" in run_script("def (", {}, timeout_s=5)["error"]
    assert "SystemExit" in run_script("raise SystemExit(3)", {}, timeout_s=5)["error"]


def test_a_long_script_is_stopped_and_tracing_is_restored():
    before = sys.gettrace()
    answer = run_script("while True:\n    pass", {}, timeout_s=0.3)
    assert not answer["ok"] and "longer than 0.3 s" in answer["error"]
    assert sys.gettrace() is before


def test_the_scripts_own_error_handling_does_not_defeat_the_limit():
    code = "while True:\n    try:\n        x = 1\n    except Exception:\n        pass"
    answer = run_script(code, {}, timeout_s=0.3)
    assert not answer["ok"] and "ScriptTimeout" in answer["error"]


def test_the_limit_does_not_fire_inside_called_functions():
    finished = []

    def slow():  # stands for a server function or a COM call made by the script
        import time

        time.sleep(0.5)
        finished.append(True)

    answer = run_script("slow()\nx = 1", {"slow": slow}, timeout_s=0.2)
    # The function ran to its end; the script stopped when it came back.
    assert finished == [True]
    assert not answer["ok"] and "line 2" in answer["error"]


def test_odd_and_large_results_are_made_safe():
    assert run_script("result = object", {}, timeout_s=5)["result"] == "<class 'object'>"
    big = run_script("result = 'x' * 50000\nprint('y' * 50000)", {}, timeout_s=5)
    assert len(big["result"]) < MAX_TEXT + 100 and "more characters" in big["output"]


@pytest.mark.anyio
async def test_no_scripts_hides_the_tool_and_status_says_so(tmp_path):
    session, _ = fake_session(tmp_path)
    mcp = build_server(session, scripts=False)
    async with Client(mcp) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        status = (await client.call_tool("lira_status", {})).structured_content
    assert "lira_run_script" not in tools and "lira_status" in tools
    assert "--no-scripts" in status["scripts"]
    assert "lira_run_script runs" not in mcp.instructions


@pytest.mark.anyio
async def test_scripts_are_on_by_default(tmp_path):
    async with Client(build_server(fake_session(tmp_path)[0])) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        status = (await client.call_tool("lira_status", {})).structured_content
    assert "lira_run_script" in tools and "scripts" not in status


@pytest.mark.anyio
async def test_script_tool_sees_the_adapter_and_backs_up(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "frame.lir"
    model.write_bytes(b"lir")
    async with Client(build_server(session)) as client:
        without = await client.call_tool("lira_run_script", {"code": "result = doc is None"})
        await client.call_tool("lira_open_model", {"path": str(model)})
        res = await client.call_tool(
            "lira_run_script",
            {"code": "adapter.write_table(doc['path'], 'Nodes_Coordinates', [[1, 0, 0, 5]])\n"
                     "result = adapter.read_table(doc['path'], 'Nodes_Coordinates')"},
        )  # fmt: skip
        broken = await client.call_tool("lira_run_script", {"code": "raise ValueError('no')"})
    assert without.structured_content["result"] is True
    assert without.structured_content["model"] is None
    answer = res.structured_content
    assert answer["ok"] and answer["result"] == [[1, 0, 0, 5]]
    assert answer["model"] == str(model) and "lira-mcp-backups" in answer["backup"]
    assert (
        not broken.is_error
        and broken.structured_content["error"] == "  line 1, in <module>\nValueError: no"
    )
