"""Checks of a model before its RPZ, on the fake adapter."""

import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.check import (
    check_model,
    coincident_nodes,
    unused_nodes,
    without_stiffness,
)
from test_rpz_tool import model

pytestmark = pytest.mark.anyio


def test_pure_checks():
    assert unused_nodes({1, 2, 3}, {1: (1, 2)}) == [3]
    assert coincident_nodes({1: (0, 0, 0), 2: (0.0004, 0, 0), 3: (1, 0, 0)}) == [[1, 2]]
    assert without_stiffness({1: 10, 2: 10, 3: 57, 4: 257}, {1: 1, 2: 0}) == [2]


def checked_model(adapter):
    model(adapter)
    tables = adapter.docs["f.lir"]["tables"]
    tables["Elements_TypeAndNodes"] = [[k, 10, f"{k}, {k + 1}"] for k in range(1, 20)] + [
        [20, 57, "1"]
    ]
    tables["Elements_Stiffnesses"] = [[k, 1] for k in range(1, 20)] + [[20, 0]]
    adapter.results_info = lambda path: {"present": True, "nodes": 20, "elements": 20}


def test_a_clean_model_passes():
    adapter = FakeAdapter()
    checked_model(adapter)
    out = check_model(adapter, "f.lir")
    bad = [f for f in out["findings"] if not f["ok"]]
    # load case 3 (wind) is in no RSN of the fake model
    assert [f["check"] for f in bad] == ["every load case is in some RSN"] and bad[0]["items"] == [
        3
    ]
    assert not out["all_ok"]


async def test_the_tool_finds_what_is_wrong(tmp_path):
    session, adapter = fake_session(tmp_path)
    checked_model(adapter)
    tables = adapter.docs["f.lir"]["tables"]
    tables["Elements_Stiffnesses"][0] = [1, 0]
    tables["Nodes_Coordinates"].append([21, 1, 0, 0])  # on node 1, in no element
    adapter.results_info = lambda path: {"present": True, "nodes": 21, "elements": 20}
    adapter.results_warnings["f.lir"] = "saved after the analysis"
    # the fake has displacements of nodes 1-20 only, as a node in no element would have none
    for name in ("node_displacements", "combination_displacements"):
        read = getattr(adapter, name)
        setattr(
            adapter, name, lambda path, ns, c, read=read: read(path, [n for n in ns if n <= 20], c)
        )
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_model_check", {})
    assert not res.is_error, res.content[0].text
    found = {f["check"]: f for f in res.structured_content["findings"]}
    assert found["every element has a stiffness"]["items"] == [1]
    assert found["no node outside elements"]["items"] == [21]
    assert found["no coincident nodes"]["items"] == [[1, 21]]
    assert not found["results not older than the model"]["ok"]
    assert found["results present and of this model"]["ok"]
