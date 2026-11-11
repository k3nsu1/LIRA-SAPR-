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
    # LIRA's results API answers in tf and tf·m: these are the kN below divided by g
    loads = {
        c: {
            n: np.array([0, 0, -100.0 * c * n, 10.0 * c, 5.0 * c, 0]) / rpz.KN_PER_TF
            for n in (1, 2)
        }
        for c in K
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


def test_load_cases_carry_gamma_f_and_the_long_term_share_from_dcl(tmp_path):
    """K2 N2: DCL holds per load case its γf and long-term share (checked on example 04)."""
    from docx import Document

    adapter = FakeAdapter()
    model(adapter)
    adapter.docs["f.lir"]["tables"]["DCL"] = [
        [None] * 9,  # LIRA's header row
        ["1", "Собственный вес", "Постоянное(П)", None, "+", "0", "1.1", "1", "0"],
        ["2", "Полезная", "Кратковременное(К)", None, "+", "0", "1.2", "0.35", "0"],
    ]
    facts = rpz.model_facts(adapter, "f.lir")
    by = {c["number"]: c for c in facts["load_cases"]}
    assert (by[1]["gamma_f"], by[1]["long_term_share"]) == (1.1, 1.0)
    assert (by[2]["gamma_f"], by[2]["long_term_share"]) == (1.2, 0.35)
    assert "gamma_f" not in by[3]  # not in DCL: nothing is made up
    table = Document(rpz.write_facts(facts, tmp_path)).tables[0]
    assert [c.text for c in table.rows[0].cells] == [
        "№",
        "Наименование",
        "Вид",
        "γf по DCL",
        "Доля длительной по DCL",
    ]
    assert [c.text for c in table.rows[2].cells][3:] == ["1,2", "0,35"]
    assert [c.text for c in table.rows[3].cells][3:] == ["", ""]


def open_text(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def test_facts_json_for_lira_plus_carries_the_model_hash_and_the_facts(tmp_path):
    """K2 N6, draft exchange format (docs/decisions/0006-lira-plus-exchange.md)."""
    import hashlib
    import json

    adapter = FakeAdapter()
    model(adapter)
    lir = tmp_path / "f.lir"
    lir.write_bytes(b"model bytes")
    facts = rpz.model_facts(adapter, "f.lir") | {"model": str(lir)}
    written = rpz.write_facts_json(facts, tmp_path)
    data = json.loads(open_text(written))
    assert (data["schema"], data["version"]) == ("lira-sapr-mcp/rpz-facts", 1)
    assert data["model"] == {
        "path": str(lir),
        "sha256": hashlib.sha256(b"model bytes").hexdigest().upper(),
    }
    assert [c["name"] for c in data["load_cases"]] == ["Собственный вес", "Полезная", "Ветер"]
    (rsn,) = data["combinations"]
    assert rsn["exact"] and [m["case"] for m in rsn["make_up"]] == [1, 2]
    assert data["stiffnesses"][0]["number"] == 1
    data = json.loads(open_text(rpz.write_facts_json(facts, tmp_path, unsaved=True)))
    assert data["model"]["unsaved_changes"] is True  # the hash is of the file, not of LIRA's


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
        p = await client.call_tool(
            "lira_footing_pressure",
            {"footings": [footing], "load_case": 1, "compression_rz_sign": -1},
        )
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


def test_loads_on_fragment_in_tf_are_checked_in_kn():
    """K1.7: the footing check took LIRA's tf for kN, so pressures came out 9.8 times low."""
    adapter = FakeAdapter()
    model(adapter)
    adapter.fragment_loads = lambda path, ns, cases: [
        {"node": n, "case": 1, "RZ": -100.0, "RUX": 2.0, "RUY": 1.0} for n in ns
    ]  # 100 tf down, as LIRA's API answers
    forces = rpz.reactions(adapter, "f.lir", [1], 1, None)
    assert forces[1]["RZ"] == pytest.approx(-980.665) and forces[1]["RUX"] == pytest.approx(19.6133)
    (f,) = rpz.footing_checks(
        [{"nodes": [1], "l": 2, "b": 2, "d": 1.5, "R": 250}], forces, compression_rz_sign=-1
    )
    assert f["N_kN"] == pytest.approx(980.7) and f["p_kPa"] == pytest.approx(275.2, abs=0.1)
    assert not f["ok"]  # 275 kPa > R = 250; with tf read as kN it was 55 kPa and "ok"


def test_proportional_cases_are_not_reported_as_exact_or_used_for_reactions():
    adapter = FakeAdapter()
    model(adapter)
    adapter.node_displacements = lambda path, ns, cases: [
        {"node": n, "case": c, "Z": n * c} for n in ns for c in (1, 2)
    ]
    adapter.combination_displacements = lambda path, ns, combos: [
        {"node": n, "combination": 5, "Z": n * 3.7} for n in ns
    ]
    row = rpz.model_facts(adapter, "f.lir")["combinations"][0]
    assert row["exact"] is False
    assert row["ambiguous_cases"] == [1, 2]
    with pytest.raises(LiraInputError, match="not uniquely determined"):
        rpz.reactions(adapter, "f.lir", [1], None, 5)


def test_the_docx_names_only_the_true_reason_an_rsn_make_up_is_unknown(tmp_path):
    """Review 2026-11-10: undetermined multipliers were also called 'dynamic or not a sum'."""
    from docx import Document

    adapter = FakeAdapter()
    model(adapter)
    adapter.node_displacements = lambda path, ns, cases: [
        {"node": n, "case": c, "Z": n * c} for n in ns for c in (1, 2)
    ]
    adapter.combination_displacements = lambda path, ns, combos: [
        {"node": n, "combination": 5, "Z": n * 3.7} for n in ns
    ]
    facts = rpz.model_facts(adapter, "f.lir")
    cell = Document(rpz.write_facts(facts, tmp_path)).tables[1].rows[1].cells[2].text
    assert cell == "Множители загружений по отдельности не определены: 1, 2"


async def test_rpz_export_warns_of_possible_edits_hidden_by_read_tracking(tmp_path):
    import json

    session, adapter = fake_session(tmp_path)
    model(adapter)
    adapter.docs["f.lir"]["possibly_unsaved"] = True
    assert not adapter.document_info().modified
    async with Client(build_server(session)) as client:
        result = await client.call_tool("lira_rpz_facts", {"output_dir": str(tmp_path)})
    assert not result.is_error, result.content[0].text
    data = json.loads((tmp_path / "f_rpz_facts.json").read_text(encoding="utf-8"))
    assert data["model"]["unsaved_changes"] is True
    assert "unsaved" in result.structured_content["warning"]


def test_unknown_sign_and_uplift_are_reported_per_footing():
    adapter = FakeAdapter()
    model(adapter)
    forces = rpz.reactions(adapter, "f.lir", [1, 2], 1, None)
    f = {"nodes": [2], "l": 2, "b": 2, "d": 1.5, "R": 250}
    (unknown,) = rpz.footing_checks([f], forces)
    assert unknown["status"] == "sign_unverified" and not unknown["ok"]
    assert unknown["RZ_kN"] == -200 and unknown["N_kN"] is unknown["p_kPa"] is None
    still_seated, lifted = rpz.footing_checks(
        [f | {"nodes": [1]}, f], forces, compression_rz_sign=1
    )
    assert still_seated["N_kN"] == -100 and still_seated["p_kPa"] == 5
    assert lifted["status"] == "uplift" and not lifted["ok"] and lifted["p_kPa"] is None


async def test_global_moments_at_the_base_and_owner_height_default(tmp_path):
    session, adapter = fake_session(tmp_path)
    model(adapter)
    adapter.units_geometry = "cm"
    adapter.docs["f.lir"]["tables"]["Nodes_Coordinates"] = [[1, 100, 200, 0], [2, 300, 200, 0]]
    adapter.fragment_loads = lambda *args: [
        {"node": 1, "case": 1, "RX": 4, "RY": 5, "RZ": -10, "RUX": 1, "RUY": 2},
        {"node": 2, "case": 1, "RX": 0, "RY": 0, "RZ": -20, "RUX": 0, "RUY": 0},
    ]
    f = {"nodes": [1, 2], "l": 8, "b": 8, "d": 1, "R": 250}
    async with Client(build_server(session)) as client:
        answer = await client.call_tool(
            "lira_footing_pressure",
            {
                "footings": [f, f | {"center": [1, 1], "h_node": 2}],
                "load_case": 1,
                "compression_rz_sign": -1,
            },
        )
    assert not answer.is_error, answer.content[0].text
    a, b = answer.structured_content["footings"]
    assert "zero rotation" in answer.structured_content["axes_requirement"]
    assert a["center_m"] == [2, 2] and a["h_node_m"] == 0
    assert a["Mx_kNm"] == round(rpz.KN_PER_TF, 1) and a["My_kNm"] == round(12 * rpz.KN_PER_TF, 1)
    assert a["Mz_kNm"] == round(-5 * rpz.KN_PER_TF, 1)
    assert a["center_source"] == "mean_node_coordinates"
    assert a["height_source"] == "nodes_at_base_by_owner_default"
    assert a["dMx_dh_kN"] == pytest.approx(-5 * rpz.KN_PER_TF)
    assert a["dMy_dh_kN"] == pytest.approx(4 * rpz.KN_PER_TF)
    assert b["Mx_kNm"] == round(-39 * rpz.KN_PER_TF, 1)
    assert b["My_kNm"] == round(50 * rpz.KN_PER_TF, 1)


def test_missing_rsn_fragment_pair_is_not_zero():
    adapter = FakeAdapter()
    model(adapter)
    original = adapter.fragment_loads
    adapter.fragment_loads = lambda *args: [
        row for row in original(*args) if (row["node"], row["case"]) != (2, 1)
    ]
    with pytest.raises(LiraInputError, match=r"node 2.*case 1"):
        rpz.reactions(adapter, "f.lir", [1, 2], None, 5)


def test_local_axes_unknown_units_and_duplicate_nodes_are_refused():
    adapter = FakeAdapter()
    model(adapter)
    adapter.units_geometry = "?"
    with pytest.raises(LiraInputError, match="geometry unit"):
        rpz.reactions(adapter, "f.lir", [1], 1, None)
    adapter.units_geometry = "m"
    adapter.docs["f.lir"]["tables"]["Nodes_LocalAxes"] = [[2, 0, 0, 1, 30]]
    with pytest.raises(LiraInputError, match="local axes"):
        rpz.reactions(adapter, "f.lir", [1, 2], 1, None)
    forces = rpz.reactions(adapter, "f.lir", [1], 1, None)
    with pytest.raises(LiraInputError, match="duplicate"):
        rpz.footing_checks([{"nodes": [1, 1], "l": 2, "b": 2, "d": 1, "R": 250}], forces)


def test_negative_height_and_invalid_sign_are_refused():
    adapter = FakeAdapter()
    model(adapter)
    forces = rpz.reactions(adapter, "f.lir", [1], 1, None)
    f = {"nodes": [1], "l": 2, "b": 2, "d": 1, "R": 250}
    with pytest.raises(LiraInputError, match="h_node"):
        rpz.footing_checks([f | {"h_node": -0.1}], forces)
    with pytest.raises(LiraInputError, match="compression_rz_sign"):
        rpz.footing_checks([f], forces, 0)


@pytest.mark.parametrize("compression_sign", [-1, 1])
def test_contact_plane_peaks_on_the_loaded_side_of_both_axes(compression_sign):
    # 500 kN, offset (+0.4, +0.3) from the centre. The loaded corner is (+X, +Y).
    f = {"nodes": [1], "l": 2, "b": 2, "d": 0, "R": 300, "center": [0, 0]}
    row = dict.fromkeys(rpz.FORCES, 0.0)
    row.update(x_m=0.4, y_m=0.3, z_m=0, RZ=compression_sign * 500.0)
    (result,) = rpz.footing_checks([f], {1: row}, compression_rz_sign=compression_sign)
    _, gradient_x, gradient_y = result["contact"]["coefficients"]
    assert gradient_x > 0 and gradient_y > 0
