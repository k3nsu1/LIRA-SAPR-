"""The member deflection tool on the fake adapter: a portal frame with a sagging beam."""

import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.members import compute, read_frame

pytestmark = pytest.mark.anyio


def portal(adapter: FakeAdapter, scale: float = 1.0) -> dict[int, tuple[float, float, float]]:
    """Two 3 m columns at x = 0 and x = 6 (bases restrained), a beam of 4 bars between their
    tops; coordinates in units of ``scale`` m. Returns X, Y, Z displacements (m): the beam
    sags 24 mm at midspan, the tops sway 10 mm along X."""
    nodes = {1: (0, 0, 0), 2: (6, 0, 0)} | {10 + i: (1.5 * i, 0, 3) for i in range(5)}
    elements = {1: (10, (1, 10)), 2: (10, (2, 14))} | {
        10 + i: (10, (10 + i, 11 + i)) for i in range(4)
    }
    adapter.docs["p.lir"] = {
        "title": "p",
        "path": "p.lir",
        "type": 5,
        "modified": False,
        "tables": {
            "Nodes_Coordinates": [
                [n, x / scale, y / scale, z / scale] for n, (x, y, z) in nodes.items()
            ],
            "Elements_TypeAndNodes": [
                [e, fe, ", ".join(map(str, ns))] for e, (fe, ns) in elements.items()
            ],
            "Nodes_Restraints": [[1, "1", "1", "1"], [2, "1", "1", "1"]],
        },
    }
    adapter.active = "p.lir"
    disp = {1: (0, 0, 0), 2: (0, 0, 0)}
    for i in range(5):
        x = 1.5 * i
        disp[10 + i] = (0.010, 0.0, -4 * 0.024 * x * (6 - x) / 36)
    return disp


def answer(disp):
    def rows(model_path, nodes, items):
        return [
            {"node": n, "combination": items[0], "X": disp[n][0], "Y": disp[n][1], "Z": disp[n][2]}
            for n in nodes
        ]

    return rows


def test_frame_spans_and_columns_from_a_combination():
    adapter = FakeAdapter()
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    frame = read_frame(adapter, "p.lir", None, None, None, 3)
    result = compute(frame)
    (span,) = result["spans"]
    assert span["kind"] == "пролёт" and span["length_m"] == pytest.approx(6.0)
    assert span["f_mm"] == pytest.approx(24.0) and span["L_to_f"] == 250 and span["node"] == 12
    assert len(result["columns"]) == 2
    assert all(c["u_mm"] == pytest.approx(10.0) and c["H_to_u"] == 300 for c in result["columns"])
    assert result["notes"] == []


def test_geometry_in_centimetres_is_read_in_metres():
    adapter = FakeAdapter()
    adapter.units_geometry = "cm"
    disp = portal(adapter, scale=0.01)
    adapter.combination_displacements = answer(disp)
    (span,) = compute(read_frame(adapter, "p.lir", None, None, None, 3))["spans"]
    assert span["length_m"] == pytest.approx(6.0)


def test_missing_displacements_and_dynamic_cases_are_refused():
    adapter = FakeAdapter()
    disp = portal(adapter)
    adapter.combination_displacements = lambda p, nodes, c: [{"node": 1, "X": 0, "Z": 0}]
    with pytest.raises(LiraInputError, match="No displacements"):
        read_frame(adapter, "p.lir", None, None, None, 3)
    adapter.node_displacements = lambda p, nodes, c: [
        {"node": n, "shape": "1", "Z": 0} for n in nodes
    ]
    with pytest.raises(LiraInputError, match="dynamic"):
        read_frame(adapter, "p.lir", None, None, 5, None)
    with pytest.raises(LiraInputError, match="No bars"):
        read_frame(adapter, "p.lir", [999], None, 5, None)
    assert disp


async def test_tool_answers_the_worst_and_writes_tables(tmp_path):
    session, adapter = fake_session(tmp_path)
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    async with Client(build_server(session)) as client:
        both = await client.call_tool("lira_member_deflections", {"combination": 3, "load_case": 1})
        assert both.is_error and "exactly one" in both.content[0].text
        res = await client.call_tool(
            "lira_member_deflections", {"combination": 3, "output_dir": str(tmp_path / "out")}
        )
    assert not res.is_error, res.content[0].text
    data = res.structured_content
    assert data["spans_total"] == 1 and data["columns_total"] == 2
    assert data["spans"][0]["L_to_f"] == 250
    names = sorted(p.split("/")[-1].split("\\")[-1] for p in data["files"])
    assert names == ["p_members.docx", "p_members.xlsx"]
    assert (tmp_path / "out" / "p_members.xlsx").stat().st_size > 0


async def test_pictures_for_the_appendices(tmp_path):
    session, adapter = fake_session(tmp_path)
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    adapter.docs["p.lir"]["tables"]["Elements_Stiffnesses"] = [
        [1, 1],
        [2, 1],
        [10, 2],
        [11, 2],
        [12, 2],
        [13, 2],
    ]
    async with Client(build_server(session)) as client:
        no_dir = await client.call_tool(
            "lira_member_deflections", {"combination": 3, "pictures": True}
        )
        bad_view = await client.call_tool(
            "lira_member_deflections", {"combination": 3, "view": "top"}
        )
        res = await client.call_tool(
            "lira_member_deflections",
            {"combination": 3, "output_dir": str(tmp_path / "out"), "pictures": True, "view": "xz"},
        )
    assert no_dir.is_error and "need output_dir" in no_dir.content[0].text
    assert bad_view.is_error and "view" in bad_view.content[0].text
    assert not res.is_error, res.content[0].text
    pngs = sorted(p.name for p in (tmp_path / "out").glob("*.png"))
    assert pngs == ["p_deformed_xz.png", "p_scheme_xz.png", "p_spans_xz.png"]
    assert all((tmp_path / "out" / n).stat().st_size > 20_000 for n in pngs)


def test_the_scheme_shows_supports_of_the_model_not_tops_of_columns():
    """K1.7: the scheme drew support triangles on the tops of the canopy columns."""
    adapter = FakeAdapter()
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    frame = read_frame(adapter, "p.lir", None, None, None, 3)
    assert {10, 14} <= frame.held  # the beam's span is still cut at the column tops
    assert frame.ground == {1, 2}


def test_blind_spans_are_named_by_their_own_elements_once():
    """K1.7: the note listed "22329, 22329, 22329" - the first bar of the beam, repeated."""
    adapter = FakeAdapter()
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    tables = adapter.docs["p.lir"]["tables"]
    # a column under every node of the beam: four spans of one bar each
    for i, n in enumerate((11, 12, 13), start=3):
        tables["Nodes_Coordinates"].append([i, 1.5 * (i - 2), 0, 0])
        tables["Elements_TypeAndNodes"].append([i, 10, f"{i}, {n}"])
        tables["Nodes_Restraints"].append([i, "1", "1", "1"])
        disp[i] = (0, 0, 0)
    result = compute(read_frame(adapter, "p.lir", None, None, None, 3))
    assert result["blind_spans"] == 4
    (note,) = [n for n in result["notes"] if n.startswith("Пролётов без")]
    assert "элементы 10, 11, 12, 13)" in note


def test_labels_of_spans_at_one_place_are_stacked():
    from lira_sapr_mcp.postprocessing.frame_plots import _label_places

    nodes = {1: (0, 0, 0), 2: (1, 0, 0), 3: (1.01, 0, 0), 4: (2, 0, 0)}
    nodes |= {5: (50, 0, 0), 6: (51, 0, 0)}
    spans = [((1, 2), 100.0), ((3, 4), 101.0), ((5, 6), 102.0)]
    shifts = [shift for _, shift in _label_places(nodes, spans, "xz", 3)]
    assert shifts == [0, 1, 0]


def test_a_span_names_its_own_bars_in_lira_notation():
    """K1.7: a rafter of bars 22308, 22311, ..., 22326 was written "22308-22326", which in
    LIRA's notation means all 19 elements between; and every span of a member named all
    of the member's bars."""
    adapter = FakeAdapter()
    disp = portal(adapter)
    adapter.combination_displacements = answer(disp)
    tables = adapter.docs["p.lir"]["tables"]
    renumber = {10: 30, 11: 33, 12: 36, 13: 37}  # beam bars 30, 33, 36, 37
    tables["Elements_TypeAndNodes"] = [
        [renumber.get(e, e), fe, ns] for e, fe, ns in tables["Elements_TypeAndNodes"]
    ]
    # a middle column under node 12 cuts the beam into two spans
    tables["Nodes_Coordinates"].append([3, 3, 0, 0])
    tables["Elements_TypeAndNodes"].append([3, 10, "3, 12"])
    tables["Nodes_Restraints"].append([3, "1", "1", "1"])
    disp[3] = (0, 0, 0)
    result = compute(read_frame(adapter, "p.lir", None, None, None, 3))
    assert sorted(s["elements"] for s in result["spans"]) == ["30 33", "36-37"]
    assert sorted(c["elements"] for c in result["columns"]) == ["1", "2", "3"]
