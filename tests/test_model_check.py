"""Checks of a model before its RPZ, on the fake adapter."""

import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.errors import LiraInputError
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


def test_without_results_the_result_checks_are_not_run_and_rsn_is_not_said_missing():
    """K1.7: before the analysis the check said "no RSN" and "not saved after the analysis"."""
    adapter = FakeAdapter()
    checked_model(adapter)
    adapter.results_info = lambda path: {"present": False}
    out = check_model(adapter, "f.lir")
    found = {f["check"]: f for f in out["findings"]}
    assert not found["results present and of this model"]["ok"]
    assert found["results not older than the model"]["ok"] is None
    assert found["every load case is in some RSN"]["ok"] is None
    assert "the model has load combinations (RSN)" not in found
    assert not out["all_ok"]


def test_special_elements_lira_adds_in_the_analysis_do_not_make_the_results_foreign():
    """K1.7: LIRA's results had 116 special elements past the model's last one."""
    adapter = FakeAdapter()
    checked_model(adapter)
    adapter.results_info = lambda path: {"present": True, "nodes": 20, "elements": 23}
    asked = []

    def forces(path, elements, cases, families=None):
        asked.append(elements)
        return [{"family": "special", "element": e, "case": 1, "section": 1} for e in elements]

    adapter.element_forces = forces
    out = check_model(adapter, "f.lir")
    found = {f["check"]: f for f in out["findings"]}
    assert asked == [[21, 22, 23]]
    assert found["results present and of this model"]["ok"]
    assert "3 служебных" in found["results present and of this model"]["note"]
    # a bar past the model's last element: results of another version of the model
    adapter.element_forces = lambda path, elements, cases, families=None: [
        {"family": "bar", "element": e, "case": 1, "section": 1} for e in elements
    ]
    out = check_model(adapter, "f.lir")
    found = {f["check"]: f for f in out["findings"]}
    assert not found["results present and of this model"]["ok"]


@pytest.mark.parametrize("a,b", [(0.0004, 0.0006), (0, 0.0009)])
@pytest.mark.parametrize("axis", [0, 1, 2])
def test_coincident_nodes_across_cell_boundaries(a, b, axis):
    first, second = [0.0] * 3, [0.0] * 3
    first[axis], second[axis] = a, b
    assert coincident_nodes({2: tuple(second), 1: tuple(first)}) == [[1, 2]]


def test_coincident_nodes_use_distance_and_transitive_groups():
    assert coincident_nodes({1: (0, 0, 0), 2: (0.001, 0, 0)}) == []
    assert coincident_nodes({1: (-0.00049,) * 3, 2: (0.00049,) * 3}) == []
    assert coincident_nodes({3: (0.0018, 0, 0), 1: (0, 0, 0), 2: (0.0009, 0, 0)}) == [[1, 2, 3]]
    assert coincident_nodes({1: (-0.0001,) * 3, 2: (0.0001,) * 3}) == [[1, 2]]


def test_coincident_nodes_of_a_large_model_take_seconds_not_minutes():
    """K2 (O5): 200 000 nodes, one pair 0.4 mm apart; 0.75 s measured on the owner's machine."""
    import time

    import numpy as np

    rng = np.random.default_rng(0)
    count = 200_000
    points = rng.uniform(0, 100, size=(count, 3)).round(3)
    xyz = {i + 1: tuple(map(float, p)) for i, p in enumerate(points)}
    xyz[count + 1] = (xyz[1][0] + 0.0004, xyz[1][1], xyz[1][2])
    started = time.perf_counter()
    groups = coincident_nodes(xyz)
    assert time.perf_counter() - started < 15
    assert groups == [[1, count + 1]]


def test_model_check_rejects_unknown_geometry_units():
    adapter = FakeAdapter()
    checked_model(adapter)
    adapter.units_geometry = "unknown"
    with pytest.raises(LiraInputError, match="Unknown geometry unit 'unknown' in LIRA"):
        check_model(adapter, "f.lir")
