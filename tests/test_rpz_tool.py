"""RPZ facts and footing pressure on the fake adapter."""

import numpy as np
import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services import rpz

pytestmark = pytest.mark.anyio

K = {1: 1 / 1.1, 2: 0.35 / 1.2, 3: 0.0}  # RSN 5: dead load, part of the live load, no wind
DOFS = ("X", "Y", "Z", "UX", "UY", "UZ")


def model(adapter: FakeAdapter, dynamic: bool = False) -> None:
    rng = np.random.default_rng(7)
    nodes = list(range(1, 21))
    adapter.docs["f.lir"] = {
        "title": "f",
        "path": "f.lir",
        "type": 5,
        "modified": False,
        "tables": {
            "Nodes_Coordinates": [[n, n, 0, 0] for n in nodes],
            "LoadCases": [
                [1, "Собственный вес", None, "Постоянное"],
                [2, "Полезная", None, "Кратковременное"],
                [3, "Ветер", None, "Кратковременное"],
            ],
            "Stiffnesses": [[1, 3, None, "Колонна 20К1"], [2, 3, None, "Балка 30Б1"]],
            "Elements_Stiffnesses": [[1, 1], [2, 1], [3, 2]],
        },
    }
    adapter.active = "f.lir"
    disp = {c: {n: rng.normal(size=6) for n in nodes} for c in K}
    loads = {
        c: {n: np.array([0, 0, -100.0 * c * n, 10.0 * c, 5.0 * c, 0]) for n in (1, 2)} for c in K
    }

    def node_displacements(path, ns, cases):
        ns = ns or nodes
        rows = [
            {"node": n, "case": c} | dict(zip(DOFS, disp[c][n], strict=True))
            for c in K
            if cases is None or c in cases
            for n in ns
        ]
        if dynamic:
            rows += [{"node": n, "case": 9, "shape": "1", "Z": 0.1} for n in ns]
        return rows

    def combination_displacements(path, ns, combos):
        total = {n: sum(K[c] * disp[c][n] for c in K) for n in ns}
        if dynamic:
            total = {n: v + 0.01 for n, v in total.items()}
        return [{"node": n, "combination": 5} | dict(zip(DOFS, total[n], strict=True)) for n in ns]

    def fragment_loads(path, ns, cases):
        return [
            {"node": n, "case": c}
            | dict(zip(("RX", "RY", "RZ", "RUX", "RUY", "RUZ"), loads[c][n], strict=True))
            for c in cases
            for n in ns
        ]

    adapter.node_displacements = node_displacements
    adapter.combination_displacements = combination_displacements
    adapter.fragment_loads = fragment_loads
    adapter.load_combinations = lambda path: {
        "code": "SP",
        "combinations": [
            {
                "number": 5,
                "name": "Нормативное длительное",
                "formula": 1,
                "special": False,
                "seismic": False,
            }
        ],
    }


def test_facts_give_cases_stiffnesses_and_the_make_up_of_an_rsn():
    adapter = FakeAdapter()
    model(adapter)
    facts = rpz.model_facts(adapter, "f.lir")
    assert [c["name"] for c in facts["load_cases"]] == ["Собственный вес", "Полезная", "Ветер"]
    assert facts["stiffnesses"] == [
        {"number": 1, "parameters": "Колонна 20К1", "elements": 2},
        {"number": 2, "parameters": "Балка 30Б1", "elements": 1},
    ]
    (rsn,) = facts["combinations"]
    assert rsn["exact"] and [m["case"] for m in rsn["make_up"]] == [1, 2]  # wind is not in it
    assert [m["multiplier"] for m in rsn["make_up"]] == [round(1 / 1.1, 4), round(0.35 / 1.2, 4)]


def test_reactions_of_an_rsn_are_its_multipliers_times_the_cases():
    adapter = FakeAdapter()
    model(adapter)
    forces = rpz.reactions(adapter, "f.lir", [1, 2], None, 5)
    expected = sum(K[c] * -100.0 * c * 2 for c in K)
    assert forces[2]["RZ"] == pytest.approx(expected)
    assert rpz.reactions(adapter, "f.lir", [1], 2, None)[1]["RZ"] == pytest.approx(-200.0)


def test_an_rsn_that_is_not_a_sum_of_static_cases_is_refused():
    adapter = FakeAdapter()
    model(adapter, dynamic=True)
    with pytest.raises(
        LiraInputError, match=r"not a sum of static load cases.*dynamic cases \[9\]"
    ):
        rpz.reactions(adapter, "f.lir", [1, 2], None, 5)
    facts = rpz.model_facts(adapter, "f.lir")
    assert facts["combinations"][0]["exact"] is False and facts["dynamic_cases_left_out"] == [9]


def test_a_footing_without_a_fragment_load_is_refused():
    with pytest.raises(LiraInputError, match="no loads on fragment at nodes \\[7\\]"):
        rpz.footing_checks([{"nodes": [7], "l": 2, "b": 2, "d": 1.5, "R": 250}], {})


async def test_tools_answer_and_write_the_tables(tmp_path):
    session, adapter = fake_session(tmp_path)
    model(adapter)
    footing = {"nodes": [2], "l": 2.0, "b": 2.0, "d": 1.5, "R": 250, "name": "Ф-1"}
    async with Client(build_server(session)) as client:
        facts = await client.call_tool("lira_rpz_facts", {"output_dir": str(tmp_path / "out")})
        p = await client.call_tool("lira_footing_pressure", {"footings": [footing], "load_case": 1})
        both = await client.call_tool(
            "lira_footing_pressure", {"footings": [footing], "load_case": 1, "combination": 5}
        )
        bad = await client.call_tool(
            "lira_footing_pressure", {"footings": [{"nodes": [2]}], "load_case": 1}
        )
    assert not facts.is_error, facts.content[0].text
    assert (tmp_path / "out" / "f_rpz_facts.docx").stat().st_size > 0
    assert not p.is_error, p.content[0].text
    (f,) = p.structured_content["footings"]
    # load case 1 at node 2: RZ = -200, RUX = 10, RUY = 5; 200 + 20 * 1.5 * 4 = 320 kN on 4 m2
    assert f["N_kN"] == 200 and f["p_kPa"] == pytest.approx(80.0) and f["name"] == "Ф-1"
    assert f["ok"] and p.structured_content["all_ok"]
    assert both.is_error and "exactly one" in both.content[0].text
    assert bad.is_error and "needs nodes, l, b, d and R" in bad.content[0].text


def test_a_load_on_a_small_part_of_a_large_model_is_found_in_the_rsn():
    """K1.7: a canopy load (case 3) moves only a few nodes of a large slab model; the even
    sample of nodes missed them and the canopy's dead load got multiplier 0."""
    adapter = FakeAdapter()
    nodes = list(range(1, 3001))
    step = len(nodes) / rpz.SAMPLE
    even = {nodes[int(i * step)] for i in range(rpz.SAMPLE)}
    canopy = [n for n in nodes if n not in even][-4:]
    rng = np.random.default_rng(3)
    disp = {
        1: {n: rng.normal(size=6) for n in nodes},
        2: {n: rng.normal(size=6) for n in nodes},
        3: {n: (rng.normal(size=6) if n in canopy else np.zeros(6)) for n in nodes},
    }
    k = {1: 1.0, 2: 0.85, 3: 1.0}
    adapter.docs["big.lir"] = {
        "title": "big",
        "path": "big.lir",
        "type": 5,
        "modified": False,
        "tables": {
            "Nodes_Coordinates": [[n, n, 0, 0] for n in nodes],
            "LoadCases": [
                [1, "Вес", None, "Постоянное"],
                [2, "Оборудование", None, "Постоянное"],
                [3, "Кровля навеса", None, "Постоянное"],
            ],
            "Stiffnesses": [],
            "Elements_Stiffnesses": [],
        },
    }
    adapter.active = "big.lir"
    adapter.node_displacements = lambda path, ns, cases: [
        {"node": n, "case": c} | dict(zip(DOFS, disp[c][n], strict=True))
        for c in (cases or k)
        for n in (ns or nodes)
    ]
    adapter.combination_displacements = lambda path, ns, combos: [
        {"node": n, "combination": 1}
        | dict(zip(DOFS, sum(k[c] * disp[c][n] for c in k), strict=True))
        for n in ns
    ]
    adapter.load_combinations = lambda path: {
        "code": "SP RK EN",
        "combinations": [
            {"number": 1, "name": "РСН1", "formula": 1, "special": False, "seismic": False}
        ],
    }
    (rsn,) = rpz.model_facts(adapter, "big.lir")["combinations"]
    assert {m["case"]: m["multiplier"] for m in rsn["make_up"]} == {1: 1.0, 2: 0.85, 3: 1.0}
    assert rsn["exact"]


def test_the_make_up_is_written_with_signs_not_plus_minus():
    make_up = [
        {"case": 1, "multiplier": 1.0},
        {"case": 2, "multiplier": 0.85},
        {"case": 3, "multiplier": -0.5},
    ]
    assert rpz.make_up_text(make_up) == "1·L1 + 0,85·L2 − 0,5·L3"
    assert rpz.make_up_text([{"case": 4, "multiplier": -1.0}]) == "−1·L4"
