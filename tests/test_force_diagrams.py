"""Diagrams of forces in bars (K2 N5): the drawing rules and the tool on the fake adapter."""

import numpy as np
import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.postprocessing import force_plots as fp
from lira_sapr_mcp.server import build_server
from test_members_tool import portal

pytestmark = pytest.mark.anyio

KN = 9.80665  # kN per tf


def test_design_sections_lie_evenly_from_the_first_node():
    assert fp.stations(2) == [0.0, 1.0]
    assert fp.stations(5) == [0.0, 0.25, 0.5, 0.75, 1.0]
    with pytest.raises(ValueError):
        fp.stations(1)


@pytest.mark.parametrize(
    ("a", "b", "normal"),
    [
        ((0, 0), (2, 0), (0, 1)),  # horizontal, numbered left to right: up
        ((2, 0), (0, 0), (0, 1)),  # numbered right to left: still up
        ((0, 0), (0, 3), (-1, 0)),  # vertical, numbered upwards: left
        ((0, 3), (0, 0), (-1, 0)),  # numbered downwards: still left
        ((0, 0), (1, 1), (-(0.5**0.5), 0.5**0.5)),  # rising: up and to the left
    ],
)
def test_positive_ordinates_go_up_or_left_whatever_the_numbering(a, b, normal):
    got = fp.side(np.array(a, float), np.array(b, float))
    assert got == pytest.approx(normal)


def test_a_bar_seen_end_on_has_no_side():
    assert fp.side(np.array([1.0, 1.0]), np.array([1.0, 1.0])) is None


def test_a_piece_that_changes_sign_is_split_at_zero():
    pieces = fp._pieces([0.0, 0.5, 1.0], [-2.0, 2.0, 2.0])
    assert pieces[0] == (0.0, -2.0, 0.25, 0.0)
    assert pieces[1] == (0.25, 0.0, 0.5, 2.0)
    assert pieces[2] == (0.5, 2.0, 1.0, 2.0)


def test_extremes_name_the_element_and_section_and_ties_go_to_the_smaller():
    hi, lo = fp.extremes({7: [1.0, 5.0], 3: [5.0, -4.0], 9: [-4.0, 0.0]})
    assert (hi.value, hi.element, hi.section) == (5.0, 3, 1)
    assert (lo.value, lo.element, lo.section) == (-4.0, 3, 2)
    assert fp.extremes({}) == (None, None)


def test_labels_are_russian_numbers_with_a_true_minus():
    assert fp.label("My", -23.858, "кН·м") == "My = −23,9 кН·м"
    assert fp.label("N", 1.234, "кН") == "N = 1,23 кН"


def test_the_largest_ordinate_is_a_set_part_of_the_drawing(tmp_path):
    nodes = {1: (0, 0, 0), 2: (6, 0, 0), 3: (6, 0, 3)}
    bars = {1: (1, 2), 2: (2, 3)}
    path, scale, end_on = fp.diagram(
        nodes, bars, {1: [-2.0, 4.0, -2.0], 2: [1.0, 1.0]}, "My", "кН·м", "xz",
        tmp_path / "d.png", "t",
    )  # fmt: skip
    assert path.stat().st_size > 10_000
    assert scale * 4.0 == pytest.approx(fp.SHARE * 6.0)
    assert end_on == []
    _, _, end_on = fp.diagram(
        nodes, bars, {1: [1.0, 1.0], 2: [1.0, 1.0]}, "N", "кН", "xy", tmp_path / "e.png", "t"
    )
    assert end_on == [2]  # the column is a point seen from above


def forces_of(values: dict[int, list[float]]):
    """element_forces of the fake: My (tf*m) per design section of each bar."""

    def rows(model_path, elements, cases, families=None):
        return [
            {"family": "bar", "element": el, "case": cases[0], "section": s, "My": v, "N": -1.0}
            for el in elements
            if el in values
            for s, v in enumerate(values[el], start=1)
        ]

    return rows


async def test_diagrams_of_a_load_case_with_extremes_in_kn(tmp_path):
    session, adapter = fake_session(tmp_path)
    portal(adapter)
    beam = {10: [-2.0, 0.5, 1.0], 11: [1.0, 1.2, 1.0], 12: [1.0, 1.2, 1.0], 13: [1.0, 0.5, -2.0]}
    adapter.element_forces = forces_of({1: [0.3, -2.0], 2: [-0.3, 2.0]} | beam)
    out = tmp_path / "out"
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_force_diagrams",
            {"load_case": 1, "output_dir": str(out), "components": ["My", "N"], "view": "xz"},
        )
    assert not res.is_error, res.content[0].text
    data = res.structured_content
    assert data["bars"] == 6
    my = data["extremes"]["My"]
    assert my["max"] == {"value": round(2.0 * KN, 3), "element": 2, "section": 2}
    assert my["min"] == {"value": round(-2.0 * KN, 3), "element": 1, "section": 2}
    assert data["extremes"]["N"]["max"]["value"] == round(-KN, 3)
    names = sorted(p.replace("\\", "/").split("/")[-1] for p in data["files"])
    assert names == ["p_My_xz.png", "p_N_xz.png"]
    assert all((out / n).stat().st_size > 10_000 for n in names)
    notes = " ".join(data.get("notes", []))
    assert "только два расчётных сечения" in notes  # the columns have 2


async def test_a_combination_is_read_in_the_chosen_limit_state(tmp_path):
    session, adapter = fake_session(tmp_path)
    portal(adapter)
    asked = {}

    def rows(model_path, elements, combinations, families, limit_states):
        asked.update(combinations=combinations, limit_states=limit_states)
        return [
            {"family": "bar", "element": el, "combination": 3, "limit_state": "serviceability",
             "section": s, "Qz": 1.0}
            for el in elements for s in (1, 2, 3)
        ]  # fmt: skip

    adapter.combination_forces = rows
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_force_diagrams",
            {"combination": 3, "limit_state": "serviceability", "components": ["Qz"],
             "output_dir": str(tmp_path)},
        )  # fmt: skip
    assert not res.is_error, res.content[0].text
    assert asked == {"combinations": [3], "limit_states": [3]}
    assert res.structured_content["source"] == {
        "combination": 3,
        "limit_state": "serviceability",
    }
    assert "notes" not in res.structured_content  # 3 sections everywhere, iso view


async def test_wrong_requests_are_refused(tmp_path):
    session, adapter = fake_session(tmp_path)
    portal(adapter)
    adapter.element_forces = forces_of({})
    out = str(tmp_path)
    async with Client(build_server(session)) as client:
        calls = [
            {"output_dir": out},
            {"output_dir": out, "load_case": 1, "combination": 2},
            {"output_dir": "rel", "load_case": 1},
            {"output_dir": out, "load_case": 1, "view": "top"},
            {"output_dir": out, "load_case": 1, "components": ["Rz"]},
            {"output_dir": out, "combination": 1, "limit_state": "ПС1"},
            {"output_dir": out, "load_case": 1},  # no forces at all
        ]
        texts = []
        for args in calls:
            res = await client.call_tool("lira_force_diagrams", args)
            assert res.is_error, args
            texts.append(res.content[0].text)
    assert "exactly one" in texts[0] and "exactly one" in texts[1]
    assert "absolute" in texts[2]
    assert "view" in texts[3] and "components" in texts[4] and "limit_state" in texts[5]
    assert "run lira_run_analysis" in texts[6]


async def test_a_dynamic_case_is_refused_and_bars_without_forces_are_named(tmp_path):
    session, adapter = fake_session(tmp_path)
    portal(adapter)

    def dynamic(model_path, elements, cases, families=None):
        return [{"family": "bar", "element": 1, "case": 4, "shape": 1, "section": 1, "My": 1.0}]

    adapter.element_forces = dynamic
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_force_diagrams", {"load_case": 4, "output_dir": str(tmp_path)}
        )
        assert res.is_error and "dynamic" in res.content[0].text
        adapter.element_forces = forces_of({10: [1.0, 2.0, 1.0]})
        res = await client.call_tool(
            "lira_force_diagrams",
            {
                "load_case": 1,
                "output_dir": str(tmp_path),
                "view": "xy",
                "elements": "1 2 10 11",
                "components": ["My"],
            },
        )
    assert not res.is_error, res.content[0].text
    data = res.structured_content
    assert data["bars"] == 4
    notes = " ".join(data["notes"])
    assert "Нет усилий у стержней: 3 (например, 1, 2, 11)" in notes


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
async def test_missing_or_nonfinite_force_is_not_drawn_as_zero(tmp_path, bad):
    session, adapter = fake_session(tmp_path)
    portal(adapter)
    adapter.element_forces = lambda *args: [
        {"family": "bar", "element": 10, "case": 1, "section": s, "My": bad} for s in (1, 2, 3)
    ]
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_force_diagrams",
            {"load_case": 1, "components": ["My"], "output_dir": str(tmp_path)},
        )
    assert res.is_error
    assert "Missing or non-finite" in res.content[0].text
