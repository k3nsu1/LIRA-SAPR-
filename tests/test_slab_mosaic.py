"""Reinforcement mosaics: scales, zone outlines and areas, DXF of zones."""

import math

import ezdxf
import numpy as np
import pytest

from lira_sapr_mcp.postprocessing.dxf import TITLE, Plan, Zone, write_zones
from lira_sapr_mcp.postprocessing.mesh import find_slabs, outline_loops
from lira_sapr_mcp.postprocessing.mosaic import (
    Bars,
    addon_scale,
    step_scale,
    zone_areas,
)
from test_slab_mesh import grid


def field_slab():
    nodes, elements, _ = grid(2, 1, size=1.0)
    (slab,) = find_slabs(nodes, elements)
    return slab


def test_a_field_plan_has_the_mean_of_the_nodes_in_each_element():
    from lira_sapr_mcp.postprocessing.dxf import field_plan

    slab = field_slab()
    node_values = -2.0 * slab.xy[:, 0]  # 0, -2, -4 mm along X
    colors = ["#000001", "#000002"]
    plan = field_plan(
        slab, node_values, np.array([-4.0, -2.0, 0.0]), colors, "T", "F", "Прогиб", "мм"
    )
    assert sorted(plan.values) == ["-1.00", "-3.00"]
    assert plan.value_layer == "F_VALUES"
    assert [(z.layer, z.color) for z in plan.zones] == [
        ("F_m4_m2", "#000001"),
        ("F_m2_0", "#000002"),
    ]
    assert plan.zones[0].text == "Прогиб: −4…−2 мм"
    assert all(len(z.loops) == 1 for z in plan.zones)


def test_a_field_plan_above_its_scale_has_a_band_of_its_own():
    from lira_sapr_mcp.postprocessing.dxf import field_plan

    slab = field_slab()
    ratio = np.where(slab.xy[:, 0] > 1.5, 3.0, 0.5)
    plan = field_plan(
        slab, ratio, np.array([0.0, 1.0]), ["#000001", "#000009"], "T", "RATIO", "f / fu"
    )
    assert [z.layer for z in plan.zones] == ["RATIO_0_1", "RATIO_over1"]
    assert plan.zones[1].text == "f / fu: больше 1"
    assert plan.values_text == "f / fu в элементе: среднее по его узлам"


def test_a_field_plan_leaves_out_an_element_with_a_node_without_a_value():
    from lira_sapr_mcp.postprocessing.dxf import field_plan

    slab = field_slab()
    values = np.where(slab.xy[:, 0] > 1.5, np.nan, 0.5)
    plan = field_plan(slab, values, np.array([0.0, 1.0]), ["#000001"], "T", "F", "Прогиб")
    assert sorted(v is None for v in plan.values) == [False, True]
    (zone,) = plan.zones
    assert zone.layer == "F_0_1"


def test_bar_area_per_meter():
    assert Bars(12, 200).area == pytest.approx(5.655, abs=0.001)
    assert Bars(16, 100).area == pytest.approx(20.106, abs=0.001)
    assert Bars(12, 200).label == "Ø12 с200"


def test_step_scale_bands_values_as_lira_does():
    s = step_scale([0, 2, 4, 6])
    values = np.array([0.0, 0.5, 2.0, 3.9, 6.0, 25.0, math.nan])
    assert s.bands(values).tolist() == [-1, 0, 1, 1, 3, 3, 4]
    assert s.labels == ["0–2", "2–4", "4–6", "≥ 6", "сечение не подобрано"]
    with pytest.raises(ValueError, match="at least two"):
        step_scale([3])


def test_addon_scale_picks_the_smallest_addon_that_suffices():
    bg = Bars(12, 200)  # 5.65
    s = addon_scale(bg, [Bars(16, 200), Bars(10, 200)])  # +3.93 = 9.58, +10.05 = 15.71
    values = np.array([0.0, 5.6, 5.7, 9.58, 9.6, 15.7, 15.8, math.nan])
    assert s.bands(values).tolist() == [0, 0, 1, 1, 2, 2, 3, 4]
    assert s.labels[0] == "фон Ø12 с200 (5,65 см²/м)"
    assert s.labels[1] == "фон + Ø10 с200 (9,58 см²/м)"
    assert s.labels[3] == "больше 15,71 см²/м"
    assert s.drawn == [False, True, True, True, True]
    assert s.codes[1] == "add_D10s200"


def test_zone_outline_and_area():
    nodes, elements, _ = grid(4, 4)
    (slab,) = find_slabs(nodes, elements)
    # an L of three cells plus a separate cell, by their lower-left corners
    picked = {(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (3.0, 3.0)}
    chosen = np.array([tuple(slab.xy[f].min(axis=0)) in picked for f in slab.faces])
    loops = outline_loops(slab, chosen)
    assert sorted(len(loop) for loop in loops) == [4, 8]
    band_of = np.where(chosen, 0, -1)
    assert zone_areas(slab, band_of, 1) == [4.0]
    inner = {(1.0, 1.0), (1.0, 2.0), (2.0, 1.0), (2.0, 2.0)}  # lower-left corners
    ring = np.array([tuple(slab.xy[f].min(axis=0)) not in inner for f in slab.faces])
    assert len(outline_loops(slab, ring)) == 2  # outer contour and the hole


def test_dxf_has_a_layer_per_zone_with_outlines_and_hatches(tmp_path):
    nodes, elements, number = grid(4, 2)
    (slab,) = find_slabs(nodes, elements, restrained={number[0, 0]})
    everything = outline_loops(slab)
    left = outline_loops(slab, np.array([slab.xy[f][:, 0].max() <= 2 for f in slab.faces]))
    path = write_zones(
        tmp_path / "z.dxf", slab, everything,
        [Plan("Плита 1: AS1", [Zone("AS1_add_D16s200", "#4e79a7", "фон + Ø16 с200", left)])],
    )  # fmt: skip
    doc = ezdxf.readfile(path)
    assert not doc.audit().has_errors  # a structurally sound DXF for drawing programs
    assert doc.header["$INSUNITS"] == 4
    msp = doc.modelspace()
    zone = msp.query('LWPOLYLINE[layer=="AS1_add_D16s200"]')
    assert len(zone) == 1
    xs = [p[0] for p in zone[0].get_points()]
    assert (min(xs), max(xs)) == (0, 2000)  # millimetres
    assert len(msp.query('HATCH[layer=="AS1_add_D16s200"]')) == 1
    assert len(msp.query('LWPOLYLINE[layer=="SLAB"]')) == 1
    assert len(msp.query('CIRCLE[layer=="SUPPORTS"]')) == 1
    assert any("Ø16" in t.dxf.text for t in msp.query("TEXT"))


def test_dxf_has_a_plan_per_component_two_in_a_row(tmp_path):
    nodes, elements, number = grid(4, 2)
    (slab,) = find_slabs(nodes, elements, restrained={number[0, 0]})
    everything = outline_loops(slab)
    plans = [
        Plan(f"Плита 1: {c}", [Zone(f"{c}_zone", "#4e79a7", f"{c}: зона", everything)])
        for c in ("AS1", "AS2", "AS3", "AS4")
    ]
    doc = ezdxf.readfile(write_zones(tmp_path / "z.dxf", slab, everything, plans))
    assert not doc.audit().has_errors
    msp = doc.modelspace()
    assert len(msp.query('LWPOLYLINE[layer=="SLAB"]')) == 4
    assert len(msp.query('CIRCLE[layer=="SUPPORTS"]')) == 4

    def box(component):
        (zone,) = msp.query(f'LWPOLYLINE[layer=="{component}_zone"]')
        xs, ys = zip(*[p[:2] for p in zone.get_points()], strict=True)
        return min(xs), max(xs), min(ys), max(ys)

    # AS1 stays at the model's coordinates, each plan is the same zone moved whole.
    x0, x1, y0, y1 = box("AS1")
    assert (x0, x1, y0, y1) == (0, 4000, 0, 2000)
    a2, a3, a4 = box("AS2"), box("AS3"), box("AS4")
    assert a2[0] > x1 and a2[2:] == (y0, y1)  # to the right
    assert a3[:2] == (x0, x1) and a3[3] < y0  # below
    assert a4[:2] == a2[:2] and a4[2:] == a3[2:]
    # Nothing of a plan reaches the next: its legend text stays left of the next plan.
    legend = [t for t in msp.query('TEXT[layer=="LEGEND"]') if "AS1:" in t.dxf.text]
    assert legend and legend[0].dxf.insert[0] + len(legend[0].dxf.text) * 150 < a2[0]
    titles = {t.dxf.text for t in msp.query('TEXT[layer=="LEGEND"]')}
    assert {"Плита 1: AS1", "Плита 1: AS4"} <= titles


def test_dxf_legend_of_a_small_slab_does_not_reach_the_plan_below(tmp_path):
    nodes, elements, _ = grid(1, 1)
    (slab,) = find_slabs(nodes, elements)
    outline = outline_loops(slab)
    zones = [Zone(f"a{k}", "#4e79a7", f"зона {k}", outline) for k in range(12)]
    plans = [Plan(c, zones) for c in ("AS1", "AS2", "AS3", "AS4")]
    msp = ezdxf.readfile(write_zones(tmp_path / "z.dxf", slab, outline, plans)).modelspace()
    texts = list(msp.query('TEXT[layer=="LEGEND"]'))
    below = next(t.dxf.insert[1] for t in texts if t.dxf.text == "AS3")
    first_row = [t.dxf.insert[1] for t in texts if t.dxf.insert[1] > below + 1]
    legends = sorted(t.dxf.insert[1] for t in texts if "зона 11" in t.dxf.text)
    assert legends[-1] == min(first_row) and legends[-1] > below + TITLE  # above the title


def test_dxf_title_of_a_small_slab_does_not_reach_the_next_plan(tmp_path):
    nodes, elements, _ = grid(1, 1)
    (slab,) = find_slabs(nodes, elements)
    outline = outline_loops(slab)
    title = "Плита 11, отм. −1,800: AS1 — нижняя арматура по X, см²/м"
    plans = [Plan(title, [Zone("a", "#4e79a7", "a", outline)]), Plan(title, [])]
    msp = ezdxf.readfile(write_zones(tmp_path / "z.dxf", slab, outline, plans)).modelspace()
    first, second = sorted(t.dxf.insert[0] for t in msp.query("TEXT") if t.dxf.text == title)
    assert second - first > len(title) * TITLE * 0.6  # 0.6: a letter's width to its height


def test_dxf_of_a_slab_on_a_foundation_leaves_out_its_supports(tmp_path):
    nodes, elements, number = grid(2, 2)
    fe = max(elements) + 1
    springs = {fe + k: (51, (n,)) for k, n in enumerate(number.values())}
    (slab,) = find_slabs(nodes, elements | springs)
    assert slab.on_foundation
    doc = ezdxf.readfile(
        write_zones(tmp_path / "z.dxf", slab, outline_loops(slab), [Plan("Плита 1", [])])
    )
    assert not doc.modelspace().query('CIRCLE[layer=="SUPPORTS"]')
