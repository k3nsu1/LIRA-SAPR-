"""Reading slabs of a LIRA model and the slab deflection tool, on the fake adapter."""

import math
from pathlib import Path

import numpy as np
import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.slabs import read_mesh, read_slabs
from test_slab_deflection import table_limit
from test_slab_mesh import grid

pytestmark = pytest.mark.anyio

# The checks below were written with limits a project may adopt: a table for deflections (6 m ->
# l/200), 150 mm and 0.003 for settlements. The server has no limit of its own (Kazakhstan: set
# per project), so the tests give them, to the tool and to the service alike.
OLD_TOOL = {"deflection_limit": 200, "settlement_limit_mm": 150, "relative_settlement_limit": 0.003}
OLD_SUMMARY = {"settlement_limit": 0.15, "relative_limit": 0.003, "fraction": 200}


def beam_slab_model(adapter: FakeAdapter, scale: float = 1.0) -> str:
    """A 6 x 3 m slab (1 m mesh) at Z 3 on walls along x = 0 and x = 6 modelled by Z
    restraints, sagging 10 mm at midspan; coordinates in units of ``scale`` m."""
    nodes, elements, number = grid(6, 3, z=3.0)
    rows = [[n, x / scale, y / scale, z / scale] for n, (x, y, z) in nodes.items()]
    els = [[e, fe, ", ".join(map(str, ns))] for e, (fe, ns) in elements.items()]
    held = [[number[i, j], None, None, "1"] for i in (0, 6) for j in range(4)]
    adapter.docs["m.lir"] = {
        "title": "m",
        "path": "m.lir",
        "type": 5,
        "modified": False,
        "tables": {
            "Nodes_Coordinates": rows,
            "Elements_TypeAndNodes": els,
            "Nodes_Restraints": held,
            "PRBs": [[1, "Все степени свободы", 1, "2 3"], [2, "Плоскость XOY", 5, "6"]],
        },
    }
    adapter.active = "m.lir"
    adapter.slab_w = {n: -0.01 * math.sin(math.pi * x / 6) for n, (x, _, _) in nodes.items()}
    return "m.lir"


def test_mesh_is_read_in_meters_with_restraints_and_rigid_bodies():
    adapter = FakeAdapter()
    adapter.units_geometry = "cm"
    ref = beam_slab_model(adapter, scale=0.01)
    mesh = read_mesh(adapter, ref)
    assert mesh.nodes[2] == pytest.approx((1.0, 0.0, 3.0))
    assert mesh.elements[1] == (44, (1, 2, 9, 8))
    assert len(mesh.restrained) == 8
    assert mesh.groups == [{1, 2, 3}]
    assert "1 rigid bodies" in mesh.notes[0].text
    assert "жёстких тел" in mesh.notes[0].ru


def test_slabs_and_their_displacements_are_read_for_a_load_case():
    adapter = FakeAdapter()
    ref = beam_slab_model(adapter)
    path, slabs, ws, _ = read_slabs(adapter, ref, None, 2, None)
    assert path == "m.lir"
    assert len(slabs) == 1 and len(ws[0]) == 28
    assert adapter.asked_vertical == (2, None)
    with pytest.raises(LiraInputError, match="No slabs among the given elements"):
        read_slabs(adapter, ref, [999], 2, None)


async def test_tool_reports_the_worst_line_of_each_slab(tmp_path):
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_deflections", OLD_TOOL | {"combination": 3})
        both = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"combination": 3, "load_case": 1}
        )
    out = res.structured_content
    assert adapter.asked_vertical == (None, 3)
    (slab,) = out["slabs"]
    assert slab["elements"] == "1-18"
    assert slab["supports"] == 10  # 8 restrained, 2 tied to a restrained node
    assert slab["deflection_mm"] == pytest.approx(-10.0)
    assert slab["span_m"] == pytest.approx(6.0)
    assert slab["allowed_mm"] == pytest.approx(30.0)
    assert slab["ratio"] == pytest.approx(0.333)
    assert slab["x_m"] == 3.0
    assert slab["nearest_node"] in {4, 11, 18, 25}
    assert slab["verdict"] == "ok"
    assert slab["panel_lows"][0]["displacement_mm"] == -10.0
    assert out["worst"] == {"slab": 1, "L_to_f": 600, "ratio": 0.333, "verdict": "ok"}
    assert out["source"] == {"combination": 3}
    assert both.is_error and "exactly one" in both.content[0].text


async def test_pictures_and_report_are_written(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    made = []

    def fake_pdf(docx, pdf):
        made.append(docx.name)
        pdf.write_bytes(b"%PDF")
        return pdf

    monkeypatch.setattr(ss.word, "docx_to_pdf", fake_pdf)
    monkeypatch.setattr(ss, "_word_trouble", [])
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    out = tmp_path / "out"
    args = {"load_case": 1, "output_dir": str(out)}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | args
        )  # a report by default
        with_png = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL
            | args
            | {"output_dir": str(tmp_path / "png"), "report": "none", "pictures": True},
        )
        relative = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | args | {"output_dir": "out", "report": "docx"}
        )
        no_dir = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"load_case": 1, "report": "pdf"}
        )
        no_dir_png = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"load_case": 1, "pictures": True}
        )
        nothing = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | args | {"report": "none"}
        )
    files = res.structured_content["files"]
    assert "pictures" not in files and not list(out.glob("*.png"))  # only on request
    assert made == ["m_slab_deflections.docx"]
    assert files["pdf"].endswith("m_slab_deflections.pdf")
    assert files["pdf_by"] == "Microsoft Word"
    doc = Document(files["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Прогибы плит относительно опор — m" in text
    assert "Перемещения: загружение 1" in text
    assert "по линиям между опорами" in text
    assert "Самый опасный участок: прогиб f = −10,00 мм на пролёте длиной l = 6,00 м" in text
    assert "(участок 1 на изополях)" in text
    assert "лежит на линии между опорными узлами" in text
    assert "Наинизших точек панелей подписано на изополе: 1" in text
    assert "что составляет l/600." in text
    assert "Предельный прогиб, заданный для проекта: fu = l/200 = 6,00 м / 200 = 30,00 мм." in text
    assert "|f| / fu = 10,00 / 30,00 = 0,33 — прогиб не превышает предельный." in text
    assert "Элементов: 18;" in text and "1-18" not in text  # the count, not the list
    row = [c.text for c in doc.tables[0].rows[1].cells]
    assert row == ["1", "+3,000", "18", "−10,00", "6,00", "30,00", "0,33", "не превышает"]
    assert len(doc.inline_shapes) == 2
    kept = with_png.structured_content["files"]
    assert [p.split("\\")[-1] for p in kept["pictures"]] == [
        "m_slab1_deflection.png",
        "m_slab1_ratio.png",
    ]
    assert all(Path(p).stat().st_size > 10_000 for p in kept["pictures"])
    assert "docx" not in kept and "pdf" not in kept
    assert relative.is_error and "absolute" in relative.content[0].text
    assert no_dir.is_error and "needs output_dir" in no_dir.content[0].text
    assert no_dir_png.is_error and "need output_dir" in no_dir_png.content[0].text
    assert nothing.is_error and "nothing to write" in nothing.content[0].text


def test_a_cantilever_and_a_lifted_point_are_explained():
    from lira_sapr_mcp.services.slabs import _how_measured

    reading = {
        "nearest_node": 7, "x_m": 1.5, "y_m": -2.0, "span_m": 4.0, "cantilever": True,
        "measured_from_nodes": [3], "point_displacement_mm": -9.0,
        "line_displacement_mm": -7.0, "deflection_mm": -2.0,
        "supports_displacement_mm": [-7.0], "direction_deg": 0.0,
    }  # fmt: skip
    text = _how_measured(reading)
    assert "Y = −2,000 м" in text
    assert "вылет от опорного узла 3 равен 2,000 м" in text
    assert "l = 2 × 2,000 = 4,00 м" in text
    assert "опоры −7,00 мм" in text
    assert "f = −9,00 − (−7,00) = −2,00 мм" in text
    assert "выгиб" not in text
    assert "ближайший узел 7" in text
    lifted = _how_measured(reading | {"point_displacement_mm": -6.0, "deflection_mm": 1.0})
    assert "Плита здесь выше опоры (выгиб)" in lifted
    plane = _how_measured(reading | {"line_displacement_mm": -8.0, "deflection_mm": -1.0})
    assert "поверхности через опоры −8,00 мм" in plane


def foundation_model(adapter):
    """The beam slab on one-node springs: nothing stands on it but what a test adds."""
    beam_slab_model(adapter)
    tables = adapter.docs["m.lir"]["tables"]
    plates = {r[0] for r in tables["Elements_TypeAndNodes"]}
    nodes = sorted(r[0] for r in tables["Nodes_Coordinates"])
    tables["Elements_TypeAndNodes"] += [
        [max(plates) + k, 51, str(n)] for k, n in enumerate(nodes, start=1)
    ]
    return tables, max(plates) + len(nodes), nodes


def stand_a_column(tables, element, node, top):
    """A bar from a slab node up to a new node ``top`` above it."""
    row = next(r for r in tables["Nodes_Coordinates"] if r[0] == node)
    tables["Nodes_Coordinates"].append([top, row[1], row[2], row[3] + 3.0])
    tables["Elements_TypeAndNodes"].append([element, 10, f"{node}, {top}"])


async def test_a_foundation_has_its_settlement_and_relative_difference(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss.word, "docx_to_pdf", lambda docx, pdf: pdf.write_bytes(b"%PDF"))
    monkeypatch.setattr(ss, "_word_trouble", [])
    session, adapter = fake_session(tmp_path)
    tables, last, nodes = foundation_model(adapter)
    # node 22 is at (0, 3), node 25 at (3, 3): settlements 2 and 5 mm
    stand_a_column(tables, last + 1, 22, 9001)
    stand_a_column(tables, last + 2, 25, 9002)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    adapter.slab_w |= {22: -0.002, 25: -0.005, 9001: -0.002, 9002: -0.005}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path / "out")},
        )
        bad = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"load_case": 1, "settlement_limit_mm": 0}
        )
    assert not res.is_error, res.content
    out = res.structured_content
    (slab,) = out["slabs"]
    assert slab["on_elastic_foundation"] and slab["node_count"] == 28
    assert "ratio" not in slab and "worst" not in out
    assert slab["verdict"] == "ok"
    assert slab["largest_displacement_mm"] == -5.0 and slab["smallest_displacement_mm"] == -2.0
    assert "on_separate_supports" not in slab
    assert slab["settlement_limit_mm"] == 150.0 and slab["zones_over_limit"] == []
    relative = slab["relative_settlement"]
    assert relative["limit"] == 0.003 and relative["columns"] == 2
    assert relative["walls_and_pylons"] == 0 and relative["within_a_wall_from_m"] == 3.0
    (place,) = relative["places"]
    assert place["ratio"] == pytest.approx(0.001)
    assert place["length_m"] == 3.0 and not place["over_limit"]
    assert {e["node"] for e in place["ends"]} == {22, 25}
    assert sorted(place["settlements_mm"]) == [-5.0, -2.0]
    doc = Document(out["files"]["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "упругие опоры у" in text and "не применяется" in text
    assert "Плита лежит на упругом основании" in text
    assert "Осадка — от −2,00 до −5,00 мм" in text
    assert "(колонн — 2, стен и пилонов — 0)" in text
    assert "узел 22 (X = 0,000 м, Y = 3,000 м)" in text
    assert "Δs/L = |−2,00 − (−5,00)| мм / 3000 мм = 0,0010 — не превышает предел." in text
    assert "предельная по абсолютной величине 150,00 мм — не превышена" in text
    assert "самых опасных участка" not in text  # no slab has spans
    assert doc.tables[0].rows[1].cells[-1].text == (
        "осадка: s = −5,0 мм; Δs/L = 0,0010: не превышает"
    )
    assert len(doc.inline_shapes) == 1
    assert bad.is_error and "above zero" in bad.content[0].text


async def test_foundation_limits_given_by_the_agent_show_what_exceeds_them(tmp_path):
    session, adapter = fake_session(tmp_path)
    tables, last, nodes = foundation_model(adapter)
    stand_a_column(tables, last + 1, 22, 9001)
    stand_a_column(tables, last + 2, 25, 9002)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    adapter.slab_w |= {22: -0.002, 25: -0.005, 9001: -0.002, 9002: -0.005}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL
            | {"load_case": 1, "settlement_limit_mm": 4, "relative_settlement_limit": 0.0005},
        )
    (slab,) = res.structured_content["slabs"]
    assert "settlement limit" in slab["verdict"] and "relative settlement" in slab["verdict"]
    assert res.structured_content["slabs_over_limits"] == [1]
    assert [z["node"] for z in slab["zones_over_limit"]] == [25]
    assert slab["zones_over_limit"][0]["settlement_mm"] == -5.0
    assert slab["relative_settlement"]["places"][0]["over_limit"]


async def test_a_drawing_of_a_slab_that_spans_has_its_deflection_and_ratio(tmp_path):
    import ezdxf

    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "none", "dxf": True},
        )
        no_folder = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"load_case": 1, "dxf": True}
        )
        nothing = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "none"},
        )
    assert not res.is_error, res.content
    assert no_folder.is_error and "needs output_dir" in no_folder.content[0].text
    assert nothing.is_error and "nothing to write" in nothing.content[0].text
    (path,) = res.structured_content["files"]["dxf"]
    assert path.endswith("m_slab1_deflection.dxf")
    assert "docx" not in res.structured_content["files"]
    doc = ezdxf.readfile(path)
    assert not doc.audit().has_errors
    msp = doc.modelspace()
    assert len(msp.query('LWPOLYLINE[layer=="SLAB"]')) == 2  # two plans
    layers = {layer.dxf.name for layer in doc.layers}
    assert {"F_VALUES", "RATIO_VALUES"} <= layers
    assert any(n.startswith("F_m") for n in layers) and any(n.startswith("RATIO_") for n in layers)
    values = [float(t.dxf.text) for t in msp.query('TEXT[layer=="F_VALUES"]')]
    assert len(values) == 18  # an element, a value
    # Midspan 10 mm down; held at both ends: elements read within that, none upward.
    assert -10 < min(values) < -5 and max(values) < 0
    assert all(float(t.dxf.text) >= 0 for t in msp.query('TEXT[layer=="RATIO_VALUES"]'))


async def test_a_drawing_of_a_slab_that_settles_has_its_settlement_in_lira_sign(tmp_path):
    import ezdxf

    session, adapter = fake_session(tmp_path)
    _, _, nodes = foundation_model(adapter)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "none", "dxf": True},
        )
    assert not res.is_error, res.content
    (path,) = res.structured_content["files"]["dxf"]
    assert path.endswith("m_slab1_settlement.dxf")
    doc = ezdxf.readfile(path)
    assert not doc.audit().has_errors
    msp = doc.modelspace()
    assert len(msp.query('LWPOLYLINE[layer=="SLAB"]')) == 1
    assert {t.dxf.text for t in msp.query('TEXT[layer=="S_VALUES"]')} == {"-3.00"}
    assert not msp.query('CIRCLE[layer=="SUPPORTS"]')  # a spring at every node


def one_node_links(tables, parameters):
    """The springs of ``foundation_model`` as links at nodes (FE 56) of one stiffness."""
    tables["Elements_TypeAndNodes"] = [
        [r[0], 56 if r[1] == 51 else r[1], r[2]] for r in tables["Elements_TypeAndNodes"]
    ]
    tables["Stiffnesses"] = [[7, 55, "#000000", None, parameters, None]]
    tables["Elements_Stiffnesses"] = [
        [r[0], 7] for r in tables["Elements_TypeAndNodes"] if r[1] == 56
    ]


@pytest.mark.parametrize(
    "parameters",
    [
        "Rx:200 Ry:200 Rz:0 Rux:0 Ruy:0 Ruz:0 DD56_END",
        "EF:1000 n:1 DD51_END",  # along X
        "EF:0 n:3 DD51_END",
    ],
)
async def test_a_one_node_element_with_no_stiffness_along_z_is_no_support(tmp_path, parameters):
    session, adapter = fake_session(tmp_path)
    tables, _, _ = foundation_model(adapter)
    one_node_links(tables, parameters)
    adapter.slab_w = {
        r[0]: -0.01 * math.sin(math.pi * r[1] / 6) for r in tables["Nodes_Coordinates"]
    }
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_deflections", OLD_TOOL | {"load_case": 1})
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    assert "on_elastic_foundation" not in slab and slab["supports"] == 10
    assert slab["deflection_mm"] == pytest.approx(-10)
    assert "nodes_on_springs" not in slab
    assert any("28 one-node elements" in n for n in res.structured_content["notes"])


@pytest.mark.parametrize(
    "parameters",
    [
        "Rx:0 Ry:0 Rz:1000 Rux:0 Ruy:0 Ruz:0 DD56_END",  # along Z
        "EF:1000 n:3 DD51_END",  # along Z
        "C:1000",  # does not say: stays what it is
    ],
)
async def test_a_one_node_element_with_stiffness_along_z_or_without_a_word_on_it_holds(
    tmp_path, parameters
):
    session, adapter = fake_session(tmp_path)
    tables, _, nodes = foundation_model(adapter)
    one_node_links(tables, parameters)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_deflections", OLD_TOOL | {"load_case": 1})
    (slab,) = res.structured_content["slabs"]
    # 8 of the 28 nodes are restrained as well, and count as restrained.
    assert slab["on_elastic_foundation"] and slab["nodes_on_springs"] == 20
    assert not any("one-node" in n for n in res.structured_content.get("notes", []))


def test_a_model_of_piles_reads_no_stiffnesses():
    from lira_sapr_mcp.services.slabs import _no_vertical_stiffness

    class NoTables:
        def read_table(self, ref, name):
            raise AssertionError(name)

    assert (
        _no_vertical_stiffness(NoTables(), "m", {1: (57, (5,)), 2: (257, (6,)), 3: (44, (1, 2))})
        == set()
    )


async def test_a_slab_on_bedding_coefficients_reports_its_settlement(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss.word, "docx_to_pdf", lambda docx, pdf: pdf.write_bytes(b"%PDF"))
    monkeypatch.setattr(ss, "_word_trouble", [])
    session, adapter = fake_session(tmp_path)
    tables, last, nodes = foundation_model(adapter)
    rows = tables["Elements_TypeAndNodes"]
    plates = [r[0] for r in rows if r[1] != 51]
    tables["Elements_TypeAndNodes"] = [r for r in rows if r[1] != 51]
    tables["Nodes_Restraints"] = []
    tables["Plates_C1C2"] = [
        [e, 1000.0 if e != plates[0] else 0.0, 0.0, 0.0, 0, 0.0, "0 - Линейная"] for e in plates
    ]
    stand_a_column(tables, last + 1, 22, 9001)
    stand_a_column(tables, last + 2, 25, 9002)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    adapter.slab_w |= {22: -0.002, 25: -0.005, 9001: -0.002, 9002: -0.005}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path / "out")},
        )
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    assert slab["on_elastic_foundation"] and "on_separate_supports" not in slab
    assert slab["plates_on_bedding"] == len(plates) - 1 and slab["nodes_on_springs"] == 0
    assert slab["supports"] == 0 and slab["largest_displacement_mm"] == -5.0
    doc = Document(res.structured_content["files"]["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert f"коэффициенты постели у {len(plates) - 1} из {len(plates)} её элементов." in text
    assert "одноузловые" not in text.split("Плита лежит на упругом основании")[1].split(".")[0]
    assert len(doc.inline_shapes) == 1


async def test_a_slab_on_separate_springs_reports_its_settlement_not_spans(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss.word, "docx_to_pdf", lambda docx, pdf: pdf.write_bytes(b"%PDF"))
    monkeypatch.setattr(ss, "_word_trouble", [])
    session, adapter = fake_session(tmp_path)
    tables, last, nodes = foundation_model(adapter)
    rows = tables["Elements_TypeAndNodes"]
    tables["Elements_TypeAndNodes"] = [r for r in rows if r[1] != 51 or int(r[2]) in nodes[:6:2]]
    tables["Nodes_Restraints"] = []  # only the springs hold it
    stand_a_column(tables, last + 1, 22, 9001)
    stand_a_column(tables, last + 2, 25, 9002)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    adapter.slab_w |= {22: -0.002, 25: -0.005, 9001: -0.002, 9002: -0.005}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path / "out")},
        )
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    assert slab["on_elastic_foundation"] and slab["on_separate_supports"]
    # Springs at nodes 1, 3, 5; the rigid body 1-2-3 of the model holds node 2 on them as well.
    assert slab["nodes_on_springs"] == 4 and slab["of_them_through_rigid_bodies"] == 1
    assert "ratio" not in slab
    assert slab["relative_settlement"]["places"][0]["ratio"] == pytest.approx(0.001)
    doc = Document(res.structured_content["files"]["docx"])
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Плита опирается на отдельные упругие опоры (сваи)" in text


def two_slabs(adapter):
    """The beam slab at Z 3 (slab 2, elements 1-18) over a copy of it at Z 0 (slab 1, 101-118)."""
    beam_slab_model(adapter)
    tables = adapter.docs["m.lir"]["tables"]
    tables["Nodes_Coordinates"] += [
        [100 + r[0], r[1], r[2], 0.0] for r in list(tables["Nodes_Coordinates"])
    ]
    tables["Elements_TypeAndNodes"] += [
        [100 + r[0], r[1], ", ".join(str(100 + int(n)) for n in r[2].split(", "))]
        for r in list(tables["Elements_TypeAndNodes"])
    ]
    tables["Nodes_Restraints"] += [[100 + r[0], *r[1:]] for r in list(tables["Nodes_Restraints"])]
    tables["PRBs"] = []
    adapter.slab_w |= {100 + n: w for n, w in list(adapter.slab_w.items())}
    adapter.plate_as = {"AS1": {e: 4.0 for r in (0, 100) for e in range(r + 1, r + 19)}}
    return tables


def names_in(folder):
    return sorted(p.name for p in folder.iterdir())


@pytest.mark.parametrize(
    "chosen",
    [list(range(1, 19)), "1-18", "selected", "visible"],
    ids=["numbers", "ranges", "selected", "visible"],
)
async def test_both_reports_on_one_slab_keep_its_number_and_the_report_on_the_model(
    tmp_path, monkeypatch, chosen
):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    two_slabs(adapter)
    doc = adapter.docs["m.lir"]
    doc["selected"] = doc["visible"] = {"elements": list(range(1, 19)), "nodes": []}
    out = tmp_path / "out"
    async with Client(build_server(session)) as client:
        everything = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(out), "report": "docx"},
        )
        slab = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL
            | {"load_case": 1, "elements": chosen, "output_dir": str(out), "report": "docx"},
        )
        mosaic = await client.call_tool(
            "lira_slab_reinforcement",
            {"elements": chosen, "output_dir": str(out), "report": "docx"},
        )
    assert [s["slab"] for s in everything.structured_content["slabs"]] == [1, 2]
    (found,) = slab.structured_content["slabs"]
    assert found["slab"] == 2 and found["elements"] == "1-18" and "part_of_slab" not in found
    assert "notes" not in slab.structured_content
    (area,) = mosaic.structured_content["slabs"]
    assert area["slab"] == 2 and "part_of_slab" not in area
    assert names_in(out) == [
        "m_slab2_deflections.docx",
        "m_slab2_reinforcement.docx",
        "m_slab2_reinforcement.dxf",
        "m_slab_deflections.docx",
    ]
    report = Document(slab.structured_content["files"]["docx"])
    assert any(p.text.startswith("Плита 2, отм.") for p in report.paragraphs)
    assert len(Document(str(out / "m_slab_deflections.docx")).tables[0].rows) == 3  # both slabs


async def test_part_of_a_slab_is_said_to_be_one(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    two_slabs(adapter)
    args = {"elements": "1-9", "output_dir": str(tmp_path / "out"), "report": "docx"}
    async with Client(build_server(session)) as client:
        slab = await client.call_tool("lira_slab_deflections", OLD_TOOL | {"load_case": 1} | args)
        mosaic = await client.call_tool("lira_slab_reinforcement", args)
    assert not slab.is_error, slab.content
    out = slab.structured_content
    (found,) = out["slabs"]
    assert found["slab"] == 2 and found["part_of_slab"] and found["whole_slab_elements"] == 18
    assert "only 9 of its 18 plates" in out["notes"][0] and "free one" in out["notes"][0]
    (area,) = mosaic.structured_content["slabs"]
    assert area["part_of_slab"] and area["whole_slab_elements"] == 18
    assert names_in(tmp_path / "out") == [
        "m_slab2_part_deflections.docx",
        "m_slab2_part_reinforcement.docx",
        "m_slab2_part_reinforcement.dxf",
    ]
    text = " ".join(p.text for p in Document(out["files"]["docx"]).paragraphs)
    assert "Плита 2 (часть), отм." in text
    assert "Выбрана часть плиты: 9 из 18 элементов." in text
    assert "край выбора считается свободным краем" in text
    made = Document(mosaic.structured_content["files"]["docx"])
    assert "Выбрана часть плиты: 9 из 18 элементов." in " ".join(p.text for p in made.paragraphs)


def test_a_report_on_many_slabs_names_them_by_their_ends():
    from types import SimpleNamespace as Slab

    from lira_sapr_mcp.services.slabs import files_name

    some = [Slab(number=n, part=False) for n in (3, 7)]
    many = [Slab(number=n, part=n == 9) for n in range(3, 13)]
    assert files_name("m", some, chosen=False) == "m_slab"
    assert files_name("m", some, chosen=True) == "m_slab3_7"
    assert files_name("m", many, chosen=True) == "m_slab3-12_part"


def test_reports_on_different_slabs_do_not_replace_each_other():
    from types import SimpleNamespace as Slab

    from lira_sapr_mcp.services.slabs import files_name

    def name(numbers):
        return files_name("m", [Slab(number=n, part=False) for n in numbers], chosen=True)

    every = name(range(1, 10))
    odd = name((1, 3, 5, 7, 9))
    other = name((1, 2, 5, 7, 9))
    assert every == "m_slab1-9" and odd.startswith("m_slab1-9_")
    assert len({every, odd, other}) == 3
    assert odd == name((1, 3, 5, 7, 9))  # the next report on them replaces this one


def settling(sizes):
    """A slab on springs at every node, with the given displacement (m) of each node by X."""
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from test_slab_mesh import grid

    nodes, elements, number = grid(4, 4, size=1.0)
    springs = {100 + k: (51, (n,)) for k, n in enumerate(number.values())}
    (slab,) = find_slabs(nodes, elements | springs)
    w = sizes(slab)
    return slab, w, relative_deflections(slab, w, table_limit)


def test_a_slab_rising_over_the_limit_is_reported_as_over_it():
    from lira_sapr_mcp.services.slabs import _foundation_lines, deflection_summary

    def up(slab):
        w = np.full(len(slab.nodes), 0.2)
        w[0] = -0.01
        return w

    slab, w, d = settling(up)
    s = deflection_summary(slab, w, d, **OLD_SUMMARY)
    assert s["largest_displacement_mm"] == 200.0 and s["smallest_displacement_mm"] == -10.0
    assert [z["settlement_mm"] for z in s["zones_over_limit"]] == [200.0]
    assert s["verdict"].startswith("exceeds the settlement limit")
    text = " ".join(_foundation_lines(s))
    assert "от −10,00 до 200,00 мм" in text and "— превышена." in text


def test_a_slab_going_down_keeps_its_range_from_the_smallest_to_the_largest():
    from lira_sapr_mcp.services.slabs import deflection_summary

    slab, w, d = settling(lambda slab: -0.001 - 0.001 * slab.xy[:, 0])
    s = deflection_summary(slab, w, d, **OLD_SUMMARY)
    assert s["largest_displacement_mm"] == -5.0 and s["smallest_displacement_mm"] == -1.0
    assert s["largest_displacement_node"] == int(slab.nodes[np.argmin(w)])


def test_the_report_tells_of_springs_through_rigid_bodies():
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _foundation_lines, deflection_summary
    from test_slab_deflection import piles_under_rigid_bodies

    nodes, elements, groups = piles_under_rigid_bodies()
    (slab,) = find_slabs(nodes, elements, groups=groups)
    w = slab.xy[:, 0] * -0.001
    s = deflection_summary(slab, w, relative_deflections(slab, w, table_limit), **OLD_SUMMARY)
    assert s["on_separate_supports"] and s["nodes_on_springs"] == 36
    assert s["of_them_through_rigid_bodies"] == 32
    assert "(у 32 из них — через абсолютно жёсткие тела)" in " ".join(_foundation_lines(s))


def test_a_few_springs_are_told_of_on_a_slab_with_no_spans():
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _slab_part, deflection_summary
    from test_slab_mesh import grid

    nodes, elements, number = grid(33, 33, size=0.5)  # held all over: no spans looked for
    corner = number[0, 0]
    restrained = set(number.values()) - {corner}
    (slab,) = find_slabs(nodes, elements | {9000: (51, (corner,))}, restrained=restrained)
    w = np.zeros(len(slab.nodes))
    s = deflection_summary(slab, w, relative_deflections(slab, w, table_limit), **OLD_SUMMARY)
    assert s["nodes_on_springs"] == 1 and s["held_all_over"] and "ratio" not in s
    assert any("это меньшая часть" in line for line in _slab_part(s, []).lines)


def test_no_line_of_springs_when_a_column_holds_the_same_rigid_body():
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _slab_part, deflection_summary
    from test_slab_deflection import one_column_and_a_body_on_a_spring

    nodes, elements, body, number = one_column_and_a_body_on_a_spring()
    (slab,) = find_slabs(
        nodes, elements, restrained={number[0, 6], number[6, 0]}, groups=[body | {number[1, 1]}]
    )
    w = slab.xy[:, 0] * -0.001
    s = deflection_summary(slab, w, relative_deflections(slab, w, table_limit), **OLD_SUMMARY)
    assert s["nodes_on_springs"] == 1 and s["springs_among_supports"] == [0, 3]
    assert not any("упругие опоры" in line for line in _slab_part(s, []).lines)


async def test_a_drawing_alone_draws_no_pictures(tmp_path, monkeypatch):
    from lira_sapr_mcp.services import slabs as ss

    def no_pictures(*_):
        raise AssertionError("nobody would see them")

    monkeypatch.setattr(ss, "_isofields", no_pictures)
    session, adapter = fake_session(tmp_path)
    _, _, nodes = foundation_model(adapter)
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "none", "dxf": True},
        )
    assert not res.is_error, res.content
    assert res.structured_content["files"]["dxf"]


@pytest.mark.parametrize(
    "bad_record", ["", None, "Rz:много DD56_END", "n:x EF:y DD51_END", "n:3 DD51_END"]
)
def test_a_stiffness_record_that_does_not_say_stays_a_support(bad_record):
    from lira_sapr_mcp.services.slabs import _holds_along_z

    assert _holds_along_z(bad_record)


def stiffness_tables(tables, stiffness, numbers):
    """FE 56 links with stiffness numbers from ``numbers(element)``, None for no row at all."""
    tables["Elements_TypeAndNodes"] = [
        [r[0], 56 if r[1] == 51 else r[1], r[2]] for r in tables["Elements_TypeAndNodes"]
    ]
    links = [r[0] for r in tables["Elements_TypeAndNodes"] if r[1] == 56]
    tables["Stiffnesses"] = stiffness
    tables["Elements_Stiffnesses"] = [
        [el, numbers(k)] for k, el in enumerate(links) if numbers(k) is not None
    ]
    return links


FREE = [7, 55, "#000000", None, "Rx:200 Ry:200 Rz:0 Rux:0 Ruy:0 Ruz:0 DD56_END", None]
HOLDS = [8, 55, "#000000", None, "Rx:0 Ry:0 Rz:900 Rux:0 Ruy:0 Ruz:0 DD56_END", None]


def test_only_the_elements_with_no_stiffness_along_z_drop_out_of_a_mix(tmp_path):
    from lira_sapr_mcp.services.slabs import read_mesh

    _, adapter = fake_session(tmp_path)
    tables, _, _ = foundation_model(adapter)
    links = stiffness_tables(tables, [FREE, HOLDS], lambda k: 7 if k % 2 else 8)
    mesh = read_mesh(adapter, "m.lir")
    assert not set(links[1::2]) & set(mesh.elements)
    assert set(links[0::2]) <= set(mesh.elements)


def test_an_element_whose_stiffness_is_not_in_the_tables_stays_a_support(tmp_path):
    from lira_sapr_mcp.services.slabs import read_mesh

    _, adapter = fake_session(tmp_path)
    tables, _, _ = foundation_model(adapter)
    # 7 has no Z; 99 is no stiffness of the model; the others have no number at all.
    links = stiffness_tables(tables, [FREE], lambda k: {0: 7, 1: 99}.get(k))
    mesh = read_mesh(adapter, "m.lir")
    assert links[0] not in mesh.elements
    assert set(links[1:]) <= set(mesh.elements)


async def test_a_slab_on_nothing_but_links_with_no_stiffness_along_z_has_no_supports(tmp_path):
    session, adapter = fake_session(tmp_path)
    tables, _, nodes = foundation_model(adapter)
    one_node_links(tables, FREE[4])
    tables["Nodes_Restraints"] = []
    adapter.slab_w = dict.fromkeys(nodes, -0.003)
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_deflections", OLD_TOOL | {"load_case": 1})
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    assert slab["supports"] == 0 and slab["verdict"].startswith("no supports found")
    assert "on_elastic_foundation" not in slab
    assert any("one-node elements" in n for n in res.structured_content["notes"])


async def test_a_column_on_a_rigid_body_is_measured_from_its_centre(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    tables, last, nodes = foundation_model(adapter)
    stand_a_column(tables, last + 1, 1, 9001)  # node 1 with 2 and 3 is a rigid body, 0 to 2 m in X
    stand_a_column(tables, last + 2, 25, 9002)  # at (3, 3)
    adapter.slab_w = dict.fromkeys(nodes, 0.0)
    adapter.slab_w |= {1: -0.001, 2: -0.002, 3: -0.003, 9001: -0.001, 9002: 0.0}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "docx"},
        )
        bad = await client.call_tool(
            "lira_slab_deflections", OLD_TOOL | {"load_case": 1, "within_a_wall_from_m": -1}
        )
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    (place,) = slab["relative_settlement"]["places"]
    centre = next(e for e in place["ends"] if e.get("rigid_body_centre"))
    assert centre == {"node": 2, "x_m": 1.0, "y_m": 0.0, "rigid_body_centre": True}
    assert place["settlements_mm"][place["ends"].index(centre)] == -2.0
    assert place["length_m"] == pytest.approx(math.hypot(2, 3), abs=0.001)
    text = "\n".join(p.text for p in Document(res.structured_content["files"]["docx"]).paragraphs)
    assert "центр АЖТ у узла 2 (X = 1,000 м, Y = 0,000 м)" in text
    assert bad.is_error and "zero or more" in bad.content[0].text


async def test_a_foundation_with_nothing_standing_on_it_says_so(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    foundation_model(adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "docx"},
        )
    (slab,) = res.structured_content["slabs"]
    assert "nothing found standing" in slab["verdict"]
    text = "\n".join(p.text for p in Document(res.structured_content["files"]["docx"]).paragraphs)
    assert "не найдены: относительная разность осадок не проверена" in text


def test_long_lists_are_cut_for_the_agent_and_whole_for_the_report():
    from lira_sapr_mcp.services.slabs import for_agent

    s = {
        "other_spans": list(range(25)),
        "panel_lows": list(range(9)),
        "zones_over_limit": [1],
        "relative_settlement": {"limit": 0.003, "places": list(range(12))},
    }
    cut = for_agent(s)
    assert len(cut["other_spans"]) == 10 and cut["other_spans_total"] == 25
    assert len(cut["panel_lows"]) == 5 and cut["panel_lows_total"] == 9
    assert cut["zones_over_limit"] == [1] and "zones_over_limit_total" not in cut
    assert len(cut["relative_settlement"]["places"]) == 10
    assert cut["relative_settlement"]["places_total"] == 12
    assert len(s["other_spans"]) == 25 and len(s["relative_settlement"]["places"]) == 12


def test_nice_levels_have_a_round_step():
    from lira_sapr_mcp.postprocessing.plots import nice_levels

    assert nice_levels(0, 10).tolist() == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert nice_levels(-0.3, 8.1).tolist()[:3] == [-1, 0, 1]
    assert nice_levels(0, 0).tolist()[0] == 0


def test_without_word_the_pdf_is_written_by_the_server(tmp_path, monkeypatch):
    from lira_sapr_mcp.adapters.word import WordUnavailable
    from lira_sapr_mcp.postprocessing.report import Report, SlabPart
    from lira_sapr_mcp.services import slabs as ss

    calls = []

    def no_word(docx, pdf):
        calls.append(docx)
        raise WordUnavailable(
            "Word did not save the PDF (it showed a window: Сбой активации)", lasting=True
        )

    monkeypatch.setattr(ss.word, "docx_to_pdf", no_word)
    monkeypatch.setattr(ss, "_word_trouble", [])
    picture = tmp_path / "p.png"
    from matplotlib.figure import Figure

    Figure(figsize=(2, 2)).savefig(picture)
    content = Report(
        "Прогибы плит", ["Модель: m.lir"], ["Плита", "f, мм"], [["1", "8,10"]],
        [SlabPart("Плита 1, отм. +3,000", ["Пролёт l = 6,00 м."], [picture])],
    )  # fmt: skip
    first = ss.write_reports(content, tmp_path / "a", "pdf")
    second = ss.write_reports(content, tmp_path / "b", "both")
    assert len(calls) == 1  # Word is not asked again in this session
    assert first == {
        "pdf": str(tmp_path / "a.pdf"),
        "pdf_by": "the server's PDF writer: Word did not save the PDF (it showed a window: "
        "Сбой активации)",
    }
    assert not (tmp_path / "a.docx").exists()
    assert (tmp_path / "a.pdf").read_bytes().startswith(b"%PDF")
    assert set(second) == {"docx", "pdf", "pdf_by"}
    assert (tmp_path / "b.pdf").stat().st_size > 1000


async def test_cracked_copy_sets_kE_of_slab_plates_in_a_copy(tmp_path):
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    original = tmp_path / "m.lir"
    original.write_bytes(b"lir")
    doc = adapter.docs.pop("m.lir")
    doc["path"] = str(original)
    doc["tables"]["Elements_TypeAndNodes"].append([99, 10, "1, 2"])  # a beam: not a slab
    adapter.docs[str(original)] = doc
    adapter.active = str(original)
    opened = adapter.open_document

    def open_copy(path):  # the copy has the tables of the file it was copied from
        info = opened(path)
        adapter.docs[path]["tables"] = {k: list(v) for k, v in doc["tables"].items()}
        return info

    adapter.open_document = open_copy
    copy = tmp_path / "m_kE0_3.lir"  # no dot: LIRA refuses one in a model name
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_cracked_copy", {"factor": 0.3})
        again = await client.call_tool("lira_cracked_copy", {"factor": 0.3, "model": str(original)})
        replaced = await client.call_tool(
            "lira_cracked_copy", {"factor": 0.3, "model": str(original), "replace": True}
        )
        bad = await client.call_tool("lira_cracked_copy", {"factor": 1.5})
        dotted = await client.call_tool(
            "lira_cracked_copy", {"factor": 0.3, "save_as": str(tmp_path / "m_kE0.3.lir")}
        )
    out = res.structured_content
    assert out["copy"] == str(copy) and copy.is_file()
    assert out["elements"] == "1-18" and out["element_count"] == 18
    written = adapter.docs[str(copy)]["tables"]["Stiffnesses_Factors"]
    assert written == [[e, 0.3] for e in range(1, 19)]
    assert "Stiffnesses_Factors" not in doc["tables"]  # the original is untouched
    assert again.is_error and "replace=true" in again.content[0].text
    assert not replaced.is_error, replaced.content
    assert "lira-mcp-backups" in replaced.structured_content["backup_of_replaced_file"]
    assert bad.is_error and "0 < kE <= 1" in bad.content[0].text
    assert dotted.is_error and "m_kE0_3" in dotted.content[0].text


async def test_reinforcement_mosaics_zones_and_report(tmp_path, monkeypatch):
    import ezdxf
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss.word, "docx_to_pdf", lambda docx, pdf: pdf.write_bytes(b"%PDF"))
    monkeypatch.setattr(ss, "_word_trouble", [])
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    # Elements 1-18: 6 across (X) by 3; bottom bars along X need 8 cm2/m in the two
    # middle columns of elements, 4 elsewhere; one element could not be designed.
    adapter.plate_as = {
        "AS1": {e: 8.0 if (e - 1) % 6 in (2, 3) else 4.0 for e in range(1, 19)},
        "AS2": {e: 1.0 for e in range(1, 19)},
    }
    adapter.plate_as["AS2"][18] = float("nan")
    out = tmp_path / "mosaic"
    args = {
        "output_dir": str(out),
        "components": ["AS1", "AS2"],
        "background": {"diameter": 10, "spacing": 200},  # 3.93
        "addons": [{"diameter": 12, "spacing": 200}, {"diameter": 10, "spacing": 200}],
        "report": "docx",
        "formats": ["png", "dxf"],
    }
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_reinforcement", args)
        steps = await client.call_tool(
            "lira_slab_reinforcement",
            {"output_dir": str(out / "steps"), "components": ["AS1"], "levels": [0, 5, 10],
             "report": "none"},
        )  # fmt: skip
        by_default = await client.call_tool(
            "lira_slab_reinforcement", {"output_dir": str(out / "plain"), "components": ["AS1"]}
        )
        nothing = await client.call_tool(
            "lira_slab_reinforcement",
            {"output_dir": str(out), "components": ["AS1"], "report": "none", "formats": []},
        )
        bad = await client.call_tool(
            "lira_slab_reinforcement", args | {"addons": None, "levels": None}
        )
    assert not res.is_error, res.content
    (slab,) = res.structured_content["slabs"]
    as1, as2 = slab["components"]["AS1"], slab["components"]["AS2"]
    assert as1["max_cm2_per_m"] == 8.0
    # 3.93 + 3.93 = 7.85 is not enough for 8: the 12 mm add-on (3.93 + 5.65 = 9.58)
    assert as1["zones_m2"] == {
        "фон + Ø10 с200 (7,85 см²/м)": 12.0,
        "фон + Ø12 с200 (9,58 см²/м)": 6.0,
    }
    assert as2["zones_m2"] == {"фон Ø10 с200 (3,93 см²/м)": 17.0, "сечение не подобрано": 1.0}
    assert as2["not_designed"] == 1
    files = res.structured_content["files"]
    assert [Path(p).name for p in files["png"]] == ["m_slab1_AS1.png", "m_slab1_AS2.png"]
    dxf = ezdxf.readfile(files["dxf"][0])
    layers = {layer.dxf.name for layer in dxf.layers}
    assert {"AS1_add_D10s200", "AS1_add_D12s200", "AS2_failed", "SLAB"} <= layers
    assert "AS2_background" not in layers  # the background is not a zone of the drawing
    msp = dxf.modelspace()
    middle = msp.query('LWPOLYLINE[layer=="AS1_add_D12s200"]')
    assert len(middle) == 1
    xs = sorted({p[0] for p in middle[0].get_points()})
    assert (xs[0], xs[-1]) == (2000, 4000)
    assert len(msp.query('LWPOLYLINE[layer=="SLAB"]')) == 2  # a plan per component
    assert {"AS1_VALUES", "AS2_VALUES"} <= layers
    written = msp.query('TEXT[layer=="AS1_VALUES"]')
    assert len(written) == 18  # one per element, in a layer of its own
    assert {t.dxf.text for t in written} == {"4.00", "8.00"}
    assert written[0].dxf.height == pytest.approx(0.22 * 1000)  # 22 % of a 1 m element
    (x, y, _) = written[0].get_placement()[1]
    assert (x, y) == (500, 500)  # the first element's centre
    assert "—" in {t.dxf.text for t in msp.query('TEXT[layer=="AS2_VALUES"]')}  # not designed
    assert msp.query('LWPOLYLINE[layer=="AS2_failed"]')[0].get_points()[0][0] > 6000  # aside
    doc = Document(files["docx"])
    assert doc.tables[0].rows[1].cells[2].text == "AS1"
    assert len(doc.inline_shapes) == 2
    s = steps.structured_content
    assert s["scale"] == ["0–5", "5–10", "≥ 10", "сечение не подобрано"]
    assert s["slabs"][0]["components"]["AS1"]["zones_m2"] == {"0–5": 12.0, "5–10": 6.0}
    assert list(s["files"]) == ["dxf"]
    assert not (out / "steps" / "m_slab1_AS1.png").exists()
    plain = by_default.structured_content["files"]
    assert {"dxf", "docx", "pdf"} <= set(plain) and "png" not in plain  # a report by default
    assert not list((out / "plain").glob("*.png"))
    assert nothing.is_error and "nothing to write" in nothing.content[0].text
    assert bad.is_error and "both background and addons" in bad.content[0].text


def saved_model(tmp_path, adapter):
    """The slab model as a saved file, open in the fake LIRA; an opened copy gets its tables."""
    beam_slab_model(adapter)
    original = tmp_path / "m.lir"
    original.write_bytes(b"the original")
    doc = adapter.docs.pop("m.lir")
    doc["path"] = str(original)
    adapter.docs[str(original)] = doc
    adapter.active = str(original)
    opened = adapter.open_document

    def open_copy(path):
        info = opened(path)
        adapter.docs[path]["tables"] = {k: list(v) for k, v in doc["tables"].items()}
        return info

    adapter.open_document = open_copy
    return original, doc


async def test_cracked_copy_keeps_the_original_and_refuses_what_would_harm_it(tmp_path):
    session, adapter = fake_session(tmp_path)
    original, doc = saved_model(tmp_path, adapter)
    async with Client(build_server(session)) as client:
        same_stem = await client.call_tool(
            "lira_cracked_copy", {"factor": 0.5, "save_as": str(tmp_path / "other" / "M.lir")}
        )
        doc["modified"] = True
        unsaved = await client.call_tool("lira_cracked_copy", {"factor": 0.5})
        doc["modified"] = False
        made = await client.call_tool("lira_cracked_copy", {"factor": 0.5})
        copy = made.structured_content["copy"]
        adapter.docs[copy]["modified"] = True
        open_unsaved = await client.call_tool(
            "lira_cracked_copy", {"factor": 0.5, "model": str(original), "replace": True}
        )
        adapter.docs[copy]["modified"] = False
        empty = await client.call_tool("lira_cracked_copy", {"factor": 0.5, "elements": []})
    assert same_stem.is_error and "other than the model's own" in same_stem.content[0].text
    assert unsaved.is_error and "Save m first" in unsaved.content[0].text
    assert open_unsaved.is_error and "unsaved changes" in open_unsaved.content[0].text
    assert empty.is_error and "empty" in empty.content[0].text
    assert original.read_bytes() == b"the original"


async def test_cracked_copy_reports_a_factor_lira_rejects(tmp_path):
    from lira_sapr_mcp.adapters.base import TableError

    session, adapter = fake_session(tmp_path)
    saved_model(tmp_path, adapter)
    adapter.write_table = lambda *_a, **_k: [TableError(1, 2, "Неверное значение")]
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_cracked_copy", {"factor": 0.5})
    assert res.is_error and "Неверное значение" in res.content[0].text
    assert str(tmp_path / "m_kE0_5.lir") not in adapter.docs  # not left open half done


async def test_cracked_copy_of_visible_elements_refuses_a_whole_model(tmp_path):
    session, adapter = fake_session(tmp_path)
    saved_model(tmp_path, adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_cracked_copy", {"factor": 0.5, "elements": "visible"})
    assert res.is_error and "fragment" in res.content[0].text.lower()


def test_report_verdict_comes_from_the_exact_ratio_not_the_rounded_one():
    from lira_sapr_mcp.services.slabs import _slab_part, _summary_row

    s = {
        "slab": 1, "z_m": 3.0, "elements": "1-4", "element_count": 4, "supports": 2,
        "largest_displacement_mm": 30.0, "largest_displacement_node": 7, "nearest_node": 7,
        "x_m": 1.0, "y_m": 1.0, "deflection_mm": 30.01, "span_m": 6.0, "allowed_mm": 30.0,
        "ratio": 1.0, "cantilever": False, "direction_deg": 0.0, "measured_from_nodes": [1, 2],
        "point_displacement_mm": -30.01, "supports_displacement_mm": [0.0, 0.0],
        "line_displacement_mm": 0.0, "other_spans": [], "panel_lows": [], "limit_fraction": 200.0,
        "verdict": "exceeds the allowed deflection",
    }  # fmt: skip
    assert _summary_row(s)[-1] == "ПРЕВЫШАЕТ"
    assert any("превышает предельный" in line and "не превышает" not in line
               for line in _slab_part(s, []).lines)  # fmt: skip


async def test_report_carries_the_notes_the_agent_hears(tmp_path, monkeypatch):
    import docx

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    adapter.results_warning = lambda _path: "saved after the results"
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 2, "output_dir": str(tmp_path), "report": "docx"},
        )
    out = res.structured_content
    assert "1 rigid bodies" in out["notes"][0] and out["warning"] == "saved after the results"
    text = "\n".join(p.text for p in docx.Document(out["files"]["docx"]).paragraphs)
    assert "жёстких тел" in text
    assert ss.STALE_RESULTS in text


async def test_mosaic_refuses_bars_without_size(tmp_path):
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_reinforcement",
            {
                "output_dir": str(tmp_path),
                "background": {"diameter": 12, "spacing": 0},
                "addons": [{"diameter": 16, "spacing": 200}],
            },
        )
    assert res.is_error and "above zero" in res.content[0].text


def beam_slab_of(sag):
    """6 m by 3 m on walls at x = 0 and x = 6, sagging across; its summary."""
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from test_slab_mesh import grid

    nodes, elements, number = grid(12, 6, size=0.5)
    held = {number[i, j] for i in (0, 12) for j in range(7)}
    (slab,) = find_slabs(nodes, elements, restrained=held)
    w = -sag * np.sin(np.pi * slab.xy[:, 0] / 6)
    return slab, w, relative_deflections(slab, w, table_limit)


def test_the_verdict_of_a_summary_comes_from_the_exact_ratio():
    from lira_sapr_mcp.services.slabs import deflection_summary

    # 30 mm is the limit of a 6 m span
    over = deflection_summary(*beam_slab_of(0.0300012), **OLD_SUMMARY)
    under = deflection_summary(*beam_slab_of(0.0299988), **OLD_SUMMARY)
    assert over["ratio"] == under["ratio"] == 1.0
    assert over["verdict"] != "ok" and under["verdict"] == "ok"
    assert over["limit_fraction"] == 200.0


def test_the_largest_displacement_is_the_largest_in_size_either_way():
    from lira_sapr_mcp.services.slabs import deflection_summary

    slab, w, d = beam_slab_of(0.01)
    up = deflection_summary(
        slab, 0.005 + 0 * w, d, **OLD_SUMMARY
    )  # all up 5 mm, the same everywhere
    assert up["largest_displacement_mm"] == 5.0
    down = deflection_summary(slab, w, d, **OLD_SUMMARY)
    assert down["largest_displacement_mm"] == -10.0
    assert down["largest_displacement_node"] == int(slab.nodes[np.argmin(w)])
    assert down["point_displacement_mm"] == -10.0


def test_a_very_short_span_does_not_lose_the_report():
    from lira_sapr_mcp.services.slabs import _worst_explained

    # the allowed deflection rounds to 0,00 mm: the limit comes from the unrounded values
    lines = _worst_explained(
        {
            "deflection_mm": -1.0, "span_m": 0.0004, "allowed_mm": 0.0, "limit_fraction": 120.0,
            "ratio": 300.0, "verdict": "exceeds", "cantilever": False, "nearest_node": 1,
            "x_m": 0.0, "y_m": 0.0, "point_displacement_mm": -1.0, "line_displacement_mm": -2.0,
            "supports_displacement_mm": [-2.0, -2.0], "measured_from_nodes": [2, 3],
            "direction_deg": 0.0,
        },
    )  # fmt: skip
    assert any("l/120 =" in line for line in lines)


def test_a_limit_at_a_table_fraction_reads_the_same_in_both_lines(tmp_path):
    from lira_sapr_mcp.services.slabs import _span_line, ru

    assert ru(-0.001) == "0,00" and ru(-1.5) == "−1,50"
    line = _span_line(
        2,
        {
            "x_m": 1.0, "y_m": 2.0, "cantilever": False, "measured_from_nodes": [1, 2],
            "span_m": 0.67, "deflection_mm": 3.0, "limit_fraction": 120.0, "allowed_mm": 5.58,
            "ratio": 0.54, "over_limit": False,
        },
    )  # fmt: skip
    assert line.startswith("Участок 2: пролёт l = 0,67 м от опорных узлов 1 и 2")
    assert "fu = 0,67 м / 120 = 5,58 мм" in line
    assert line.endswith("|f| / fu = 0,54 — не превышает предельный.")
    assert "выгиб" in line


def test_a_place_listed_over_the_limit_reads_so_even_at_a_rounded_one():
    from lira_sapr_mcp.services.slabs import _span_line, deflection_summary

    over = deflection_summary(*beam_slab_of(0.0300012), **OLD_SUMMARY)
    under = deflection_summary(*beam_slab_of(0.0299988), **OLD_SUMMARY)
    assert over["ratio"] == under["ratio"] == 1.0
    for s, word in ((over, "— превышает предельный."), (under, "— не превышает предельный.")):
        assert _span_line(1, s).endswith(word)


async def test_the_reinforcement_answer_says_what_was_not_taken_as_a_support(tmp_path):
    session, adapter = fake_session(tmp_path)
    tables, _, _ = foundation_model(adapter)
    one_node_links(tables, FREE[4])
    adapter.plate_as = {"AS1": {e: 4.0 for e in range(1, 19)}}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_reinforcement",
            {"output_dir": str(tmp_path / "out"), "components": ["AS1"], "levels": [0, 5, 10],
             "report": "none", "formats": ["dxf"]},
        )  # fmt: skip
    assert not res.is_error, res.content
    assert any("28 one-node elements" in n for n in res.structured_content["notes"])


def test_each_slab_has_its_own_scale(tmp_path, monkeypatch):
    from lira_sapr_mcp.postprocessing import plots
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _isofields, deflection_summaries
    from test_slab_mesh import grid

    nodes, elements, number = grid(4, 4, size=1.0)
    springs = {100 + k: (51, (n,)) for k, n in enumerate(number.values())}
    one, one_elements, one_number = grid(12, 4, size=0.5, z=3.0, first_node=1000, first_el=1000)
    two, two_elements, two_number = grid(12, 4, size=0.5, z=6.0, first_node=2000, first_el=2000)
    held = {n[i, j] for n in (one_number, two_number) for i in (0, 12) for j in range(5)}
    ground, low, high = find_slabs(
        nodes | one | two, elements | springs | one_elements | two_elements, restrained=held
    )
    assert ground.on_foundation and not low.on_foundation
    near_uniform = -0.200 + 1e-16 * np.arange(len(ground.nodes))  # settlement, near uniform
    ws = [
        near_uniform,
        -0.01 * np.sin(np.pi * low.xy[:, 0] / 6),
        -0.002 * np.sin(np.pi * high.xy[:, 0] / 6),
    ]
    slabs = [ground, low, high]
    found = [relative_deflections(s, w, table_limit) for s, w in zip(slabs, ws, strict=True)]
    seen = {}
    real = plots.isofield

    def spy(slab, values, levels, path, *args, **kwargs):
        seen[path.name] = levels
        return real(slab, values, levels, path, *args, **kwargs)

    monkeypatch.setattr(plots, "isofield", spy)
    summaries = deflection_summaries(slabs, ws, found, **OLD_SUMMARY)
    pictures = _isofields(slabs, ws, found, summaries, tmp_path, "m")
    assert [p.name for p in pictures[1]] == ["m_slab1_settlement.png"]
    assert [p.name for p in pictures[2]] == ["m_slab2_deflection.png", "m_slab2_ratio.png"]
    assert -20 < seen["m_slab2_deflection.png"][0] <= -10
    assert -4 < seen["m_slab3_deflection.png"][0] <= -2
    assert seen["m_slab2_deflection.png"][-1] == 0
    assert seen["m_slab2_ratio.png"][-1] == seen["m_slab3_ratio.png"][-1] == 1  # 0-1 on every slab
    assert seen["m_slab1_settlement.png"][0] <= -200


def test_supports_without_spans_are_not_reported_as_no_supports():
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _slab_part, _summary_row, deflection_summary

    # One triangle held at its three nodes: they are all next to each other.
    nodes = {1: (0.0, 0.0, 0.0), 2: (1.0, 0.0, 0.0), 3: (0.0, 1.0, 0.0)}
    (slab,) = find_slabs(nodes, {1: (42, (1, 2, 3))}, restrained={1, 2, 3})
    w = np.zeros(3)
    s = deflection_summary(slab, w, relative_deflections(slab, w, table_limit), **OLD_SUMMARY)
    assert s["verdict"].startswith("no spans")
    assert _summary_row(s)[-1] == "нет пролётов"
    assert "Ни одна линия между опорами" in " ".join(_slab_part(s, []).lines)


async def test_a_foundation_with_one_column_has_nothing_to_compare(tmp_path, monkeypatch):
    from docx import Document

    from lira_sapr_mcp.services import slabs as ss

    monkeypatch.setattr(ss, "_word_trouble", ["no Word in this test"])
    session, adapter = fake_session(tmp_path)
    tables, last, _ = foundation_model(adapter)
    stand_a_column(tables, last + 1, 22, 9001)
    adapter.slab_w[9001] = -0.01
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_slab_deflections",
            OLD_TOOL | {"load_case": 1, "output_dir": str(tmp_path), "report": "docx"},
        )
    (slab,) = res.structured_content["slabs"]
    assert "one short structure stands" in slab["verdict"]
    text = "\n".join(p.text for p in Document(res.structured_content["files"]["docx"]).paragraphs)
    assert "сравнивать не с чем" in text


def test_a_slab_on_few_columns_has_its_pictures_though_most_of_it_has_no_value(tmp_path):
    from lira_sapr_mcp.postprocessing.deflection import relative_deflections
    from lira_sapr_mcp.postprocessing.mesh import find_slabs
    from lira_sapr_mcp.services.slabs import _isofields, deflection_summaries
    from test_slab_mesh import grid

    nodes, elements, number = grid(12, 12, size=0.5)
    corners = {number[i, j] for i in (0, 12) for j in (0, 12)}
    (slab,) = find_slabs(nodes, elements, restrained=corners)
    w = -0.02 * np.sin(np.pi * slab.xy[:, 0] / 6) * np.sin(np.pi * slab.xy[:, 1] / 6)
    found = [relative_deflections(slab, w, table_limit)]
    assert np.isnan(found[0].f).any()
    pictures = _isofields(
        [slab], [w], found, deflection_summaries([slab], [w], found, **OLD_SUMMARY), tmp_path, "m"
    )
    assert all(p.stat().st_size > 10_000 for p in pictures[1])


def test_each_picture_has_a_page_of_its_own_turned_as_it_fits(tmp_path):
    import re

    from docx import Document
    from docx.enum.section import WD_ORIENT
    from matplotlib.figure import Figure

    from lira_sapr_mcp.postprocessing.report import Report, SlabPart, to_docx, to_pdf

    wide, tall = tmp_path / "wide.png", tmp_path / "tall.png"
    Figure(figsize=(8, 4)).savefig(wide)
    Figure(figsize=(4, 8)).savefig(tall)
    content = Report(
        "Прогибы", ["Модель"], ["Плита"], [["1"]], [SlabPart("Плита 1", ["Текст"], [wide, tall])]
    )
    doc = Document(to_docx(content, tmp_path / "a.docx"))
    orientation = [s.orientation for s in doc.sections]
    assert orientation == [
        WD_ORIENT.PORTRAIT,
        WD_ORIENT.PORTRAIT,
        WD_ORIENT.LANDSCAPE,
        WD_ORIENT.PORTRAIT,
    ]
    assert len(doc.inline_shapes) == 2
    across = doc.inline_shapes[0]
    assert across.width.cm == pytest.approx(26.7, abs=0.1)  # the width of the sheet less margins
    written = to_pdf(content, tmp_path / "a.pdf").read_bytes()
    sizes = re.findall(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", written)
    assert any(float(a) > float(b) for a, b in sizes)  # a page across
    assert any(float(a) < float(b) for a, b in sizes)  # and pages upright


async def test_without_limits_the_answer_is_facts_only(tmp_path):
    session, adapter = fake_session(tmp_path)
    beam_slab_model(adapter)
    async with Client(build_server(session)) as client:
        res = await client.call_tool("lira_slab_deflections", {"load_case": 1})
    assert not res.is_error, res.content
    out = res.structured_content
    (slab,) = out["slabs"]
    assert slab["verdict"] == "no limit set: facts only"
    assert slab["L_to_f"] == 600 and slab["deflection_mm"] == pytest.approx(-10.0)
    assert "ratio" not in slab and "allowed_mm" not in slab
    assert out["worst"] == {"slab": 1, "L_to_f": 600, "verdict": "no limit set: facts only"}
    assert out["limits"] == {"deflection": None, "settlement_mm": None, "relative_settlement": None}
    assert "slabs_over_limits" not in out


async def test_a_structure_of_table_v1_sets_the_settlement_limits(tmp_path):
    session, adapter = fake_session(tmp_path)
    foundation_model(adapter)
    async with Client(build_server(session)) as client:
        steel = await client.call_tool(
            "lira_slab_deflections", {"load_case": 1, "structure": "steel_frame"}
        )
        override = await client.call_tool(
            "lira_slab_deflections",
            {"load_case": 1, "structure": "steel_frame", "settlement_limit_mm": 180},
        )
        wrong = await client.call_tool(
            "lira_slab_deflections", {"load_case": 1, "structure": "tower"}
        )
        bare = await client.call_tool("lira_slab_deflections", {"load_case": 1})
    (slab,) = steel.structured_content["slabs"]
    assert slab["settlement_limit_mm"] == 150.0 and slab["relative_settlement"]["limit"] == 0.004
    assert "СП РК 5.01-102-2013" in steel.structured_content["limits"]["structure"]
    (slab,) = override.structured_content["slabs"]
    assert slab["settlement_limit_mm"] == 180.0 and slab["relative_settlement"]["limit"] == 0.004
    assert wrong.is_error and "steel_frame" in wrong.content[0].text
    (slab,) = bare.structured_content["slabs"]
    assert (
        slab["verdict"].startswith("no limit set: facts only")
        and slab["settlement_limit_mm"] is None
    )
