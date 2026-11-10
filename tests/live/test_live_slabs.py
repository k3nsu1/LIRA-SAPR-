"""Acceptance example 8: slab deflection relative to supports against hand calculation.

Slabs of 20 cm concrete (E = 3e6 t/m2, nu = 0.2: D = E h^3 / 12 (1 - nu^2) = 2083.3 t*m)
under 1 t/m2, built in LIRA's text format:

- a slab 6 m across, 12 m long, on two lines of Z supports: it bends as a cylinder
  (its long edges may not turn, as if the slab went on), w = 5 q L^4 / 384 D =
  8.10 mm over a 6 m span;
- a cantilever 2 m out from a clamped edge, 8 m long, likewise: at the tip
  w = q L^4 / 8 D = 0.96 mm, span 4 m (twice the overhang);
- a three-storey frame with a 6 x 6 m slab on four 40 x 40 cm columns per storey: the
  slab spans its diagonal, and its deflection is the absolute displacement less that
  of the columns (they shorten under the storeys above).
"""

import math
from pathlib import Path

import numpy as np
import pytest
from mcp import Client
from test_live_selection import user_selects

from lira_sapr_mcp.postprocessing.deflection import relative_deflections
from lira_sapr_mcp.services.slabs import read_slabs

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

D = 3e6 * 0.2**3 / (12 * (1 - 0.2**2))


def slab_text(name, nx, ny, size, levels, held, columns=(), extra_restraints="", links=()):
    """Text of a model of type 5: slabs of nx x ny shells (FE 44) of ``size`` m at each level,
    1 t/m2 on every slab; ``held``: node -> restraint directions; ``columns``: (i, j)
    grid points with a 40 x 40 cm column under each slab; ``links``: (i, j) grid points of the
    first slab with a one-node link (FE 56) of stiffness 3, which the text gives after the rest."""
    per_level = (nx + 1) * (ny + 1)

    def node(i, j, k):
        return 1 + i + (nx + 1) * j + per_level * k

    coords = [
        f"{i * size:g} {j * size:g} {3 * k:g}"
        for k in range(len(levels) + (1 if columns else 0))
        for j in range(ny + 1)
        for i in range(nx + 1)
    ]
    plates, bars = [], []
    first = 1 if columns else 0
    for k in range(first, first + len(levels)):
        for j in range(ny):
            for i in range(nx):
                # LIRA's text lists a quad's nodes crosswise: two of one side, then the
                # two of the opposite side in the same direction (example 02: 1 2 8 9).
                ns = (node(i, j, k), node(i + 1, j, k), node(i, j + 1, k), node(i + 1, j + 1, k))
                plates.append("44 1 " + " ".join(map(str, ns)))
        for i, j in columns:
            bars.append(f"10 2 {node(i, j, k - 1)} {node(i, j, k)}")
    bars += [f"56 3 {node(i, j, first)}" for i, j in links]
    restraints = [f"{n} {dirs}/" for n, dirs in held(node).items()]
    loads = [f"{e} 16 3 1 1/" for e in range(1, len(plates) + 1)]
    text = "\n".join(
        [
            f"( 0/ 1; {name}/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )",
            "( 1/ " + " ".join(f"{e}/" for e in plates + bars) + " )",
            "( 3/ 1 GEI 3e6 0.2 0.2 RO 0/ 2 S0 3E6 40 40/ )",
            "( 4/ " + " ".join(f"{c}/" for c in coords) + " )",
            "( 5/ " + " ".join(restraints) + extra_restraints + " )",
            "( 6/ " + " ".join(loads) + " )",
            "( 7/ 1 1 0 0 0/ )",
        ]
    )
    return text + "\n"


async def analysed(client, text, path):
    made = await client.call_tool("lira_import_text_model", {"text": text, "save_as": path})
    assert not made.is_error, made.content
    res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
    assert not res.is_error and res.structured_content["status"] == "finished", res.content
    return res.structured_content


# The server has no limits of its own (Kazakhstan: set per project, K1.2): the checks give a
# project's, as the offline tests do (tests/test_slabs_tool.py, OLD_TOOL).
PROJECT = {"deflection_limit": 200, "settlement_limit_mm": 150, "relative_settlement_limit": 0.003}


async def deflections(client, path, **args):
    res = await client.call_tool(
        "lira_slab_deflections", {"model": path, "load_case": 1} | PROJECT | args
    )
    assert not res.is_error, res.content
    return res.structured_content


async def test_beam_slab_and_cantilever_match_plate_theory(mcp, workdir):
    def beam_held(node):
        # Only bending: in-plane and drilling freedoms are fixed everywhere.
        held = {node(i, j, 0): "1 2 6" for j in range(25) for i in range(13)}
        held |= {node(i, j, 0): "1 2 4 6" for j in (0, 24) for i in range(13)}
        held |= {node(i, j, 0): "1 2 3 6" for j in range(25) for i in (0, 12)}
        return held

    def lever_held(node):
        held = {node(i, j, 0): "1 2 6" for j in range(33) for i in range(9)}
        held |= {node(i, j, 0): "1 2 4 6" for j in (0, 32) for i in range(9)}
        held |= {node(0, j, 0): "1 2 3 4 5 6" for j in range(33)}
        return held

    beam = slab_text("lmcp_slab_beam", 12, 24, 0.5, [0], beam_held)
    lever = slab_text("lmcp_slab_lever", 8, 32, 0.25, [0], lever_held)
    beam_path = str(workdir / "lmcp_slab_beam.lir")
    lever_path = str(workdir / "lmcp_slab_lever.lir")
    async with Client(mcp) as client:
        await analysed(client, beam, beam_path)
        await analysed(client, lever, lever_path)
        b = await deflections(client, beam_path)
        c = await deflections(client, lever_path)
    (slab,) = b["slabs"]
    assert slab["supports"] == 50
    assert slab["deflection_mm"] == pytest.approx(-5 * 6**4 / (384 * D) * 1000, rel=0.02)
    assert slab["span_m"] == pytest.approx(6)
    assert slab["allowed_mm"] == pytest.approx(30)
    assert slab["x_m"] == pytest.approx(3)
    assert not slab["cantilever"]
    assert slab["verdict"] == "ok"
    (slab,) = c["slabs"]
    assert slab["cantilever"]
    assert slab["x_m"] == pytest.approx(2)
    assert slab["span_m"] == pytest.approx(4)
    assert slab["deflection_mm"] == pytest.approx(-(2**4) / (8 * D) * 1000, rel=0.03)
    assert slab["allowed_mm"] == pytest.approx(4000 / 200)  # the project's l/200 of 4 m
    print(f"example 8: beam slab {b['slabs'][0]}, cantilever {slab}")


async def test_slab_on_columns_of_a_multistorey_frame(mcp, workdir):
    import ezdxf

    corners = [(0, 0), (12, 0), (0, 12), (12, 12)]

    def held(node):
        return {node(i, j, 0): "1 2 3 4 5 6" for i, j in corners}

    text = slab_text("lmcp_slab_frame", 12, 12, 0.5, [1, 2, 3], held, columns=corners)

    def xy(n):  # plan position of node n of this grid
        i, j = (n - 1) % 13, (n - 1) % 169 // 13
        return 0.5 * i, 0.5 * j

    path = str(workdir / "lmcp_slab_frame.lir")
    async with Client(mcp) as client:
        await analysed(client, text, path)
        out = await deflections(client, path, output_dir=str(workdir), report="both", dxf=True)
        top = out["slabs"][2]
        moved = await client.call_tool(
            "lira_node_displacements",
            {
                "model": path,
                "nodes": [top["nearest_node"], *top["measured_from_nodes"]],
                "components": ["Z"],
                "top": None,
            },
        )
    assert [s["z_m"] for s in out["slabs"]] == [3, 6, 9]
    assert [s["supports"] for s in out["slabs"]] == [4, 4, 4]
    assert not any(s["cantilever"] for s in out["slabs"])
    a, b = top["measured_from_nodes"]
    assert {xy(a), xy(b)} <= {(0, 0), (6, 0), (0, 6), (6, 6)}
    rows = moved.structured_content["rows"]
    to_mm = {"mm": 1, "cm": 10, "m": 1000}[moved.structured_content["units"]["Z"]]
    z = {r["node"]: r["Z"] * to_mm for r in rows}
    # By hand: the worst point of the diagonal is the panel's centre, a node, and it lies on
    # the line; the slab there less the line between the two columns.
    (ax, ay), (bx, by), (px, py) = xy(a), xy(b), (top["x_m"], top["y_m"])
    span = math.hypot(bx - ax, by - ay)
    assert xy(top["nearest_node"]) == pytest.approx((px, py))
    assert abs((px - ax) * (by - ay) - (py - ay) * (bx - ax)) / span < 1e-9
    t = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / span**2
    line = z[a] + t * (z[b] - z[a])
    assert top["span_m"] == pytest.approx(span, abs=0.001)
    point = z[top["nearest_node"]]
    assert top["point_displacement_mm"] == pytest.approx(point, abs=0.01)
    assert top["deflection_mm"] == pytest.approx(point - line, abs=0.01)
    # On a multi-storey frame the columns shorten: the deflection relative to them is
    # less than the point's absolute displacement by the displacement of the line.
    assert z[a] < -0.05 and z[b] < -0.05  # mm
    assert top["deflection_mm"] - point == pytest.approx(-line, abs=0.01)
    print(f"example 8: top slab of the frame {top}, columns moved {z[a]:.3f} mm")
    # Example 9 in small: pictures of the three slabs and the report, the PDF by Word
    # where it works, else by the server's own writer.
    files = out["files"]
    assert "pictures" not in files  # PNG only on request; the report holds the isofields
    for name in [files["docx"], files["pdf"]]:
        assert Path(name).stat().st_size > 5_000, name
    assert Path(files["pdf"]).read_bytes().startswith(b"%PDF")
    print(f"example 9 (small): PDF by {files['pdf_by']}")
    assert len(files["dxf"]) == 3
    for name in files["dxf"]:
        assert not ezdxf.readfile(name).audit().has_errors
    drawing = ezdxf.readfile(files["dxf"][2]).modelspace()
    values = [float(t.dxf.text) for t in drawing.query('TEXT[layer=="F_VALUES"]')]
    assert len(values) == 144  # an element, a value
    # A mean of nodes stays within what the nodes read, and near the largest of them.
    assert top["deflection_mm"] - 0.01 <= min(values) < 0


async def test_on_rigid_points_the_field_is_lira_displacement_in_every_node(
    mcp, lira, adapter, workdir
):
    """The owner's check: 3 x 2 spans of 6 m on points held in Z, 1.5 m overhangs."""
    points = [(3 + 12 * a, 3 + 12 * b) for a in range(4) for b in range(3)]

    def held(node):
        out = {node(i, j, 0): "1 2 6" for j in range(31) for i in range(43)}
        return out | {node(i, j, 0): "1 2 3 6" for i, j in points}

    path = str(workdir / "lmcp_slab_rigid.lir")
    async with Client(mcp) as client:
        await analysed(client, slab_text("lmcp_slab_rigid", 42, 30, 0.5, [0], held), path)
    _, (slab,), (w,), _ = lira(read_slabs, adapter, path, None, 1, None)
    assert np.abs(w[slab.supports]).max() < 1e-9
    assert np.abs(w).max() > 0.001
    d = relative_deflections(slab, w)
    free = np.ones(len(w), dtype=bool)
    free[slab.supports] = False
    assert np.isfinite(d.f[free]).all()
    assert d.f[free] == pytest.approx(w[free], abs=1e-9)


async def one_node_elements(client, workdir, name, fe, stiffness):
    """A slab on four points held in Z and 25 one-node elements, most of its supports if they
    count; only bending is free. The answer on it."""
    links = [(i, j) for i in range(2, 11, 2) for j in range(2, 11, 2)]

    def held(node):
        out = {node(i, j, 0): "1 2 6" for j in range(13) for i in range(13)}
        return out | {node(i, j, 0): "1 2 3 6" for i, j in [(0, 0), (12, 0), (0, 12), (12, 12)]}

    text = (
        slab_text(name, 12, 12, 0.5, [0], held, links=links)
        .replace("2 S0 3E6 40 40/", f"2 S0 3E6 40 40/ 3 {stiffness}/")
        .replace("56 3 ", f"{fe} 3 ")
    )
    path = str(workdir / f"{name}.lir")
    await analysed(client, text, path)
    return await deflections(client, path)


@pytest.mark.parametrize(
    ("fe", "stiffness", "settles"),
    [
        (56, "200 200 0 0 0 0", False),
        (56, "200 200 1000 0 0 0", True),
        (51, "1 1000", False),
        (51, "3 1000", True),
    ],
    ids=["fe56_in_plan", "fe56_along_z", "fe51_along_x", "fe51_along_z"],
)
async def test_a_one_node_element_holds_the_slab_only_with_stiffness_along_z(
    mcp, workdir, fe, stiffness, settles
):
    name = f"lmcp_slab_links_{fe}_{settles}"
    async with Client(mcp) as client:
        out = await one_node_elements(client, workdir, name, fe, stiffness)
    (slab,) = out["slabs"]
    assert slab.get("on_elastic_foundation", False) is settles
    if settles:
        assert slab["nodes_on_springs"] == 25 and slab["on_separate_supports"]
    else:
        assert slab["supports"] == 4 and "ratio" in slab
        assert any("25 one-node elements" in n for n in out["notes"])


async def test_direction_3_of_fe_51_is_z(mcp, workdir):
    """What the reading above rests on: elements along direction 3 hold the slab up, those along
    direction 1 do not."""
    async with Client(mcp) as client:
        along_z = await one_node_elements(client, workdir, "lmcp_slab_dir3", 51, "3 1000")
        along_x = await one_node_elements(client, workdir, "lmcp_slab_dir1", 51, "1 1000")
    held = abs(along_z["slabs"][0]["largest_displacement_mm"])
    free = abs(along_x["slabs"][0]["largest_displacement_mm"])
    print(f"FE 51: the slab moves {held} mm along direction 3, {free} mm along direction 1")
    assert held < free / 2


EXAMPLE_09 = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")


async def test_links_of_example_09_with_no_stiffness_along_z_are_no_supports(
    lira, adapter, workdir
):
    """LIRA's own example: 320 links with Rx and Ry alone."""
    import shutil

    from lira_sapr_mcp.services.slabs import open_model, read_mesh

    found = list(EXAMPLE_09.glob("09_*/*.lir"))
    if not found:
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex09.lir")
    shutil.copy(found[0], path)
    mesh = lira(lambda: read_mesh(adapter, open_model(adapter, path)))
    assert not [el for el, (fe, _) in mesh.elements.items() if fe == 56]
    assert any("320 one-node elements" in n.text for n in mesh.notes)


async def test_report_on_the_slab_selected_in_the_window(mcp, lira, adapter, workdir):
    corners = [(0, 0), (12, 0), (0, 12), (12, 12)]

    def held(node):
        return {node(i, j, 0): "1 2 3 4 5 6" for i, j in corners}

    text = slab_text("lmcp_slab_chosen", 12, 12, 0.5, [1, 2, 3], held, columns=corners)
    path = str(workdir / "lmcp_slab_chosen.lir")
    out = workdir / "lmcp_slab_chosen_out"
    async with Client(mcp) as client:
        await analysed(client, text, path)
        everything = await deflections(client, path)
        lira(user_selects, adapter, path, "289-432")  # the plates of the top slab
        top = await deflections(client, path, elements="selected", output_dir=str(out))
        lira(user_selects, adapter, path, "289-300")  # its first row
        part = await deflections(client, path, elements="selected", output_dir=str(out))
    (slab,) = top["slabs"]
    assert slab["slab"] == 3 and "part_of_slab" not in slab
    assert slab["deflection_mm"] == everything["slabs"][2]["deflection_mm"]
    assert Path(top["files"]["docx"]).name == "lmcp_slab_chosen_slab3_deflections.docx"
    (piece,) = part["slabs"]
    assert piece["slab"] == 3 and piece["part_of_slab"] and piece["whole_slab_elements"] == 144
    assert "only 12 of its 144 plates" in " ".join(part["notes"])
    assert Path(part["files"]["docx"]).name == "lmcp_slab_chosen_slab3_part_deflections.docx"


async def test_cracked_copy_with_half_the_modulus_deflects_twice_as_much(mcp, workdir):
    def held(node):
        held = {node(i, j, 0): "1 2 6" for j in range(9) for i in range(13)}
        held |= {node(i, j, 0): "1 2 4 6" for j in (0, 8) for i in range(13)}
        held |= {node(i, j, 0): "1 2 3 6" for j in range(9) for i in (0, 12)}
        return held

    path = str(workdir / "lmcp_slab_crack.lir")
    async with Client(mcp) as client:
        await analysed(client, slab_text("lmcp_slab_crack", 12, 8, 0.5, [0], held), path)
        whole = await deflections(client, path)
        made = await client.call_tool("lira_cracked_copy", {"model": path, "factor": 0.5})
        assert not made.is_error, made.content
        copy = made.structured_content["copy"]
        res = await client.call_tool("lira_run_analysis", {"model": copy, "timeout_s": 300})
        assert res.structured_content["status"] == "finished", res.content
        cracked = await deflections(client, copy)
        factors = await client.call_tool(
            "lira_read_table", {"model": copy, "table": "Stiffnesses_Factors", "numbers": "1-3"}
        )
    assert copy.endswith("lmcp_slab_crack_kE0_5.lir")
    assert made.structured_content["element_count"] == 96
    assert [r[1] for r in factors.structured_content["rows"]] == [0.5, 0.5, 0.5]
    before, after = whole["slabs"][0]["deflection_mm"], cracked["slabs"][0]["deflection_mm"]
    assert after == pytest.approx(2 * before, rel=0.005)
    print(f"cracks: kE 0.5 doubles the deflection {before} -> {after} mm")


EXAMPLE_02 = Path(
    r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU\02_плита ЖБ\02_плита ЖБ.lir"
)
EXAMPLE_22 = Path(
    r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU"
    r"\22_пространственный каркас на свайном основании - ГРУНТ\22_3D каркас_свайное основание.lir"
)
EXAMPLE_04 = Path(
    r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU"
    r"\04_пространственный каркас с фунд_плитой на упругом основании"
    r"\04_3D каркас_упругое основание.lir"
)


async def test_a_slab_on_piles_of_example_22_settles(mcp, lira, adapter, workdir):
    """LIRA's own example: a raft on separate one-node piles reports its settlement in LIRA's
    sign, not spans between the piles; the floor above it still spans between its columns."""
    import shutil

    import ezdxf
    from docx import Document

    if not EXAMPLE_22.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex22.lir")
    shutil.copy(EXAMPLE_22, path)
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
        assert res.structured_content["status"] == "finished", res.content
        out = await deflections(client, path, output_dir=str(workdir), dxf=True)
        moved = await client.call_tool(
            "lira_node_displacements",
            {"model": path, "nodes": [out["slabs"][0]["largest_displacement_node"]],
             "load_cases": [1], "components": ["Z"], "top": None},
        )  # fmt: skip
    raft, floor = out["slabs"]
    assert raft["on_elastic_foundation"] and raft["on_separate_supports"]
    assert raft["nodes_on_springs"] == 31 and raft["node_count"] == 289
    assert "ratio" not in raft and "ratio" in floor
    low = raft["largest_displacement_mm"]
    assert low < raft["smallest_displacement_mm"] < 0
    (row,) = moved.structured_content["rows"]
    to_mm = {"mm": 1, "cm": 10, "m": 1000}[moved.structured_content["units"]["Z"]]
    assert low == pytest.approx(row["Z"] * to_mm, abs=0.01)
    text = "\n".join(p.text for p in Document(out["files"]["docx"]).paragraphs)
    assert "Плита опирается на отдельные упругие опоры (сваи)" in text
    settles, spans = out["files"]["dxf"]
    assert settles.endswith("lmcp_ex22_slab1_settlement.dxf")
    assert spans.endswith("lmcp_ex22_slab2_deflection.dxf")
    drawing = ezdxf.readfile(settles)
    assert not drawing.audit().has_errors
    assert len(drawing.modelspace().query('CIRCLE[layer=="SUPPORTS"]')) == 31
    rows = lira(adapter.read_table, path, "Elements_TypeAndNodes")
    one_node = {r[1] for r in rows if len(str(r[2]).replace(",", " ").split()) == 1}
    print(f"example 22: one-node element types {one_node}, raft {raft}")
    assert one_node


async def test_a_slab_on_bedding_coefficients_of_example_04_settles(mcp, workdir):
    """LIRA's own example: a raft with C1 on its plates and nothing under it, which has no
    supports to measure from: it reports its settlement, and the floors above their spans."""
    import shutil

    from docx import Document

    if not EXAMPLE_04.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex04.lir")
    shutil.copy(EXAMPLE_04, path)
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
        assert res.structured_content["status"] == "finished", res.content
        out = await deflections(client, path, output_dir=str(workdir))
    raft, *floors = out["slabs"]
    assert raft["on_elastic_foundation"] and raft["plates_on_bedding"] == raft["element_count"]
    assert raft["supports"] == 0 and "on_separate_supports" not in raft
    assert raft["largest_displacement_mm"] < raft["smallest_displacement_mm"] < 0
    assert raft["relative_settlement"]["places"]
    assert all("ratio" in floor for floor in floors)
    text = "\n".join(p.text for p in Document(out["files"]["docx"]).paragraphs)
    assert "коэффициенты постели у" in text
    print(f"example 04: raft {raft}")


async def test_reinforcement_mosaics_of_example_02(mcp, lira, adapter, workdir):
    """Acceptance example 10 in part: mosaics of LIRA's reinforced concrete slab agree
    with the areas LIRA reports per element; the DXF is sound. (Comparing with LIRA's
    own mosaic picture and opening the DXF in a drawing program is the owner's check.)"""
    import shutil

    import ezdxf

    if not EXAMPLE_02.exists():
        pytest.skip("LIRA example models are not installed")
    path = str(workdir / "lmcp_ex02.lir")
    shutil.copy(EXAMPLE_02, path)
    levels = [0, 1, 2, 3, 4, 6, 8]
    async with Client(mcp) as client:
        await client.call_tool("lira_open_model", {"path": path})
        res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
        assert res.structured_content["status"] == "finished", res.content
        mosaic = await client.call_tool(
            "lira_slab_reinforcement",
            {"model": path, "output_dir": str(workdir), "levels": levels, "report": "pdf"},
        )
        assert not mosaic.is_error, mosaic.content
        from_lira = await client.call_tool(
            "lira_reinforcement", {"model": path, "sort_by": "AS1", "top": 1}
        )
        lira(user_selects, adapter, path, "1-20")
        chosen = await client.call_tool(
            "lira_slab_reinforcement",
            {"model": path, "elements": "selected", "output_dir": str(workdir / "lmcp_ex02_part"),
             "levels": levels, "report": "docx"},
        )  # fmt: skip
    assert not chosen.is_error, chosen.content
    (piece,) = chosen.structured_content["slabs"]
    assert piece["slab"] == 1 and piece["part_of_slab"] and piece["element_count"] == 20
    assert Path(chosen.structured_content["files"]["docx"]).name == (
        "lmcp_ex02_slab1_part_reinforcement.docx"
    )
    out = mosaic.structured_content
    (slab,) = out["slabs"]
    top = from_lira.structured_content["families"]["plate"]["rows"][0]
    as1 = slab["components"]["AS1"]
    assert as1["max_cm2_per_m"] == pytest.approx(top["AS1"], abs=0.01)
    assert as1["max_element"] == top["element"]
    files = out["files"]
    assert "png" not in files and len(files["dxf"]) == 1
    doc = ezdxf.readfile(files["dxf"][0])
    assert not doc.audit().has_errors
    assert any(layer.dxf.name.startswith("AS1_") for layer in doc.layers)
    assert len(doc.modelspace().query('LWPOLYLINE[layer=="SLAB"]')) % 4 == 0  # four plans
    assert Path(files["pdf"]).read_bytes().startswith(b"%PDF")
    print(f"example 10: {slab}")
