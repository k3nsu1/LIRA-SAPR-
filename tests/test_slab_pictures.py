"""The pictures: a slab drawn alone and large, spans marked, a key plan beside it, and no
support markers over a slab on a foundation. Checked through what the figure holds."""

import numpy as np
import pytest
from matplotlib.collections import LineCollection

from lira_sapr_mcp.postprocessing import plots
from lira_sapr_mcp.postprocessing.mesh import find_slabs
from test_slab_mesh import grid


def slabs_and_figure(monkeypatch):
    """A small slab beside a large one (no shared nodes); the figures that get saved."""
    nodes, elements, number = grid(2, 2, z=0.0)
    far = {n + 1000: (x + 50.0, y, z) for n, (x, y, z) in nodes.items()}
    big = {e + 1000: (fe, tuple(n + 1000 for n in ns)) for e, (fe, ns) in elements.items()}
    held = {number[0, 0], number[2, 2]}
    small, large = find_slabs(nodes | far, elements | big, restrained=held)
    figures = []
    original = plots._save

    def keep(fig, path):
        figures.append(fig)
        return original(fig, path)

    monkeypatch.setattr(plots, "_save", keep)
    return small, large, figures


def test_a_slab_fills_its_picture_and_the_key_plan_finds_it(tmp_path, monkeypatch):
    small, large, figures = slabs_and_figure(monkeypatch)
    site = small.site
    assert site is large.site
    plots.isofield(
        small, np.arange(9.0), np.arange(0.0, 9.5, 1.0), tmp_path / "a.png", "Плита", "мм",
        site=site,
    )  # fmt: skip
    main, key, *_ = figures[0].axes
    # The plan stops at the slab, not at the other slab 50 m away.
    assert main.get_xlim()[1] < 3 and main.get_xlim()[0] > -1
    # The key plan holds them all, with this slab drawn apart.
    assert key.get_xlim()[1] > 50
    outlines = [c for c in key.collections if isinstance(c, LineCollection)]
    assert len(outlines) == 2
    assert len(outlines[0].get_segments()) == len(site)
    assert len(outlines[1].get_segments()) == len(small.boundary)
    assert (tmp_path / "a.png").stat().st_size > 5_000


def test_one_slab_has_no_key_plan(monkeypatch, tmp_path):
    small, _, figures = slabs_and_figure(monkeypatch)
    nodes, elements, _ = grid(2, 2)
    assert find_slabs(nodes, elements)[0].site is None
    bands = np.zeros(len(small.faces), int)
    plots.mosaic(small, ["#ff0000"], bands, ["a"], tmp_path / "m.png", "M")
    key = figures[0].axes[1]
    assert not key.axison and not key.collections


def test_marks_show_their_lines_and_labels_stay_apart(tmp_path, monkeypatch):
    small, _, figures = slabs_and_figure(monkeypatch)
    marks = [
        plots.Mark((1.0, 1.0), "1: f = 10,0 мм, l = 2,00 м", np.array([[0.0, 0.0], [2.0, 2.0]])),
        plots.Mark((1.0, 1.1), "2: f = 9,0 мм, l = 2,00 м", np.array([[0.0, 2.0], [2.0, 0.0]])),
        plots.Mark((1.1, 1.0), "3: f = 8,0 мм, вылет 1,00 м", np.array([[2.0, 0.0], [1.1, 1.0]])),
    ]
    plots.isofield(
        small, np.arange(9.0), np.arange(0.0, 9.5, 1.0), tmp_path / "a.png", "Плита", "мм", marks
    )
    ax = figures[0].axes[0]
    notes = [t for t in ax.texts if t.get_text()[:2] in ("1:", "2:", "3:")]
    assert len(notes) == 3
    renderer = figures[0].canvas.get_renderer()
    boxes = [t.get_window_extent(renderer) for t in notes]
    assert not any(a.overlaps(b) for i, a in enumerate(boxes) for b in boxes[i + 1 :])
    lines = [ln for ln in ax.get_lines() if len(ln.get_xdata()) == 2 and ln.get_linewidth() == 1.4]
    assert [ln.get_xydata().tolist() for ln in lines] == [m.line.tolist() for m in marks]


def test_no_support_markers_over_a_slab_on_a_foundation(tmp_path, monkeypatch):
    nodes, elements, number = grid(2, 2)
    springs = {100 + k: (51, (n,)) for k, n in enumerate(number.values())}
    (ground,) = find_slabs(nodes, elements | springs)
    (slab,) = find_slabs(nodes, elements, restrained={number[0, 0]})
    figures = []
    original = plots._save

    def keep(fig, path):
        figures.append(fig)
        return original(fig, path)

    monkeypatch.setattr(plots, "_save", keep)
    for s in (ground, slab):
        plots.isofield(s, np.arange(9.0), np.arange(0.0, 9.5, 1.0), tmp_path / "a.png", "П", "мм")
    assert not figures[0].axes[0].get_lines()
    assert figures[1].axes[0].get_lines()


def test_support_markers_stay_over_a_slab_on_separate_springs(tmp_path, monkeypatch):
    nodes, elements, number = grid(6, 6)
    piles = {100 + k: (51, (number[i, 3],)) for k, i in enumerate((0, 3, 6))}
    (slab,) = find_slabs(nodes, elements | piles)
    assert slab.on_foundation and not slab.on_soil
    figures = []
    original = plots._save

    def keep(fig, path):
        figures.append(fig)
        return original(fig, path)

    monkeypatch.setattr(plots, "_save", keep)
    values = -slab.xy[:, 0]
    plots.isofield(slab, values, np.linspace(-7, 0, 8), tmp_path / "a.png", "П", "мм")
    assert figures[0].axes[0].get_lines()


def test_a_slab_settling_past_the_limit_all_over_is_outlined_in_red(tmp_path, monkeypatch):
    slab = small_slab()
    collections = []
    real = plots.LineCollection

    def spy(segments, **kwargs):
        collections.append(kwargs.get("colors"))
        return real(segments, **kwargs)

    monkeypatch.setattr(plots, "LineCollection", spy)
    values = -200.0 - slab.xy[:, 0]
    levels = np.linspace(-210, -190, 5)
    plots.isofield(slab, values, levels, tmp_path / "a.png", "П", "мм", limit=150.0,
                   down_is_red=True)  # fmt: skip
    assert plots.MARK_COLOR in collections


@pytest.mark.parametrize(
    ("lowest", "highest", "contours"),
    [(-200.0, 200.0, [-150.0, 150.0]), (-200.0, 10.0, [-150.0]), (-10.0, 200.0, [150.0])],
)
def test_the_limit_is_drawn_where_the_slab_reaches_it_either_way(
    tmp_path, monkeypatch, lowest, highest, contours
):
    from matplotlib.axes import Axes

    drawn = []
    real = Axes.tricontour

    def spy(self, *args, **kwargs):
        if kwargs.get("colors") == plots.MARK_COLOR:
            drawn.append(list(kwargs["levels"]))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "tricontour", spy)
    slab = small_slab()
    x = slab.xy[:, 0]
    values = lowest + (highest - lowest) * (x - x.min()) / np.ptp(x)
    levels = plots.nice_levels(lowest, highest)
    plots.isofield(slab, values, levels, tmp_path / "a.png", "П", "мм", limit=150.0)
    assert drawn == [contours]


def test_the_colours_of_a_drawing_are_those_of_the_isofield(tmp_path, monkeypatch):
    from matplotlib.axes import Axes
    from matplotlib.colors import to_hex

    used = {}
    real = Axes.tricontourf

    def spy(self, *args, **kwargs):
        used["colors"] = [to_hex(c) for c in kwargs["cmap"].colors]
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "tricontourf", spy)
    slab = small_slab()
    levels = plots.nice_levels(-10.0, 0.0)
    plots.isofield(slab, -slab.xy[:, 0], levels, tmp_path / "a.png", "П", "мм", down_is_red=True)
    assert used["colors"] == plots.band_colors(len(levels) - 1, True)


@pytest.mark.parametrize("lowest", [-0.42, -0.79, -1.53, -1.9])
def test_a_level_at_zero_has_no_sign(lowest):
    levels = plots.nice_levels(lowest, 0.0)
    assert not np.signbit(levels[levels == 0]).any()


def small_slab():
    nodes, elements, _ = grid(2, 2)
    (slab,) = find_slabs(nodes, elements)
    return slab


def test_a_picture_with_no_element_fully_on_lines_has_its_scale_and_marks(tmp_path):
    slab = small_slab()
    values = np.full(len(slab.nodes), np.nan)
    path = plots.isofield(
        slab, values, plots.RATIO_LEVELS, tmp_path / "empty.png", "Плита", "f / fu",
        [plots.Mark((1.0, 1.0), "1: f/fu = 1,20", np.array([[0.0, 0.0], [2.0, 2.0]]))],
        over_red=True,
    )  # fmt: skip
    assert path.stat().st_size > 5_000


def test_a_slab_all_over_the_limit_has_its_outline_red(tmp_path):
    slab = small_slab()
    values = 200.0 + slab.xy[:, 0]
    levels = plots.nice_levels(float(values.min()), float(values.max()))
    path = plots.isofield(slab, values, levels, tmp_path / "over.png", "Плита", "мм", limit=150.0)
    assert path.stat().st_size > 5_000


def test_many_marks_are_not_moved_around(tmp_path, monkeypatch):
    # Trying each place for each label took minutes on a slab with hundreds of them. Counted, not
    # timed: Windows may slow this process down as much as it likes.
    from matplotlib.text import Annotation

    tried = []
    move = Annotation.set_position
    monkeypatch.setattr(
        Annotation, "set_position", lambda self, xy: (tried.append(xy), move(self, xy))[1]
    )
    slab = small_slab()
    marks = [plots.Mark((0.01 * k, 1.0), str(k)) for k in range(200)]
    values = slab.xy[:, 0] + 0.0
    plots.isofield(slab, values, plots.nice_levels(0, 2), tmp_path / "m.png", "Плита", "мм", marks)
    assert 200 <= len(tried) <= 400  # one place each, and the one it ends up in


def test_the_picture_of_a_slab_is_turned_as_it_comes_out_larger(tmp_path, monkeypatch):
    from matplotlib.image import imread

    across = find_slabs(*grid(20, 4)[:2])[0]
    upright = find_slabs(*grid(4, 20)[:2])[0]
    shapes = []
    for k, slab in enumerate((across, upright)):
        values = slab.xy[:, 0] + 0.0
        levels = plots.nice_levels(0, float(values.max()))
        path = plots.isofield(slab, values, levels, tmp_path / f"{k}.png", "Плита", "мм")
        shapes.append(imread(path).shape)
    assert shapes[0][1] > shapes[0][0]
    assert shapes[1][0] > shapes[1][1]


def test_the_largest_deflection_down_is_at_the_hot_end_of_the_scale(tmp_path, monkeypatch):
    small, _, figures = slabs_and_figure(monkeypatch)
    levels = np.arange(-9.0, 0.5, 1.0)
    values = -np.arange(9.0)
    plots.isofield(small, values, levels, tmp_path / "a.png", "П", "мм")
    plots.isofield(small, values, levels, tmp_path / "b.png", "П", "мм", down_is_red=True)
    plain, turned = (fig.axes[0].collections[0].cmap for fig in figures)
    assert turned(0) == plain(plain.N - 1)
    assert turned(turned.N - 1) == plain(0)
