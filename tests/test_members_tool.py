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
