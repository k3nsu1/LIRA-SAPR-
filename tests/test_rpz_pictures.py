"""Pictures of the RPZ appendices (lira_rpz_pictures): the plan as in the owner's RPZ
sample, LIRA's list of load cases, and the tool on the fake adapter."""

import json

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.adapters import result_view as rv
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services import rpz_pictures as rp

pytestmark = pytest.mark.anyio

ITEMS = ["1. РСН1", "2. РСН2", "3. РСН3", "MAXIMUM", "MINIMUM", "MAX ABS"]


def test_an_item_of_lira_list_is_found_by_number_or_envelope_and_never_guessed():
    assert rv.item_index(ITEMS, 2) == 1
    assert rv.item_index(ITEMS, "MINIMUM") == 4
    assert rv.item_index(["10. РСН10", "1. РСН1"], 1) == 1  # not "10." for 1
    with pytest.raises(LiraInputError, match=r"has no 7; it has: 1\. РСН1"):
        rv.item_index(ITEMS, 7)
    with pytest.raises(LiraInputError):
        rv.item_index(ITEMS, "MAX")


def test_the_list_is_that_of_the_results_only_with_envelopes():
    assert rv.ready(ITEMS, rsn=True) and rv.ready(ITEMS, rsn=False)
    assert not rv.ready(["1. Загружение 1", "2. Ветер"], rsn=False)  # input mode
    assert not rv.ready(["1. Загружение 1", "MAXIMUM"], rsn=True)  # not yet switched to RSN


def test_a_mosaic_is_told_from_the_bare_scheme_by_its_colours():
    def picture(colours):
        rows = []
        for k in range(80):  # 80 rows of 50 pixels, each row one colour
            b, g, r = colours[k % len(colours)]
            rows += [b, g, r, 255] * 50
        return 50, 80, bytes(rows)

    scheme = [(255, 255, 255), (0, 160, 0), (255, 0, 0)]  # white, green and blue lines
    mosaic = [(0, 32 * k, 255) for k in range(8)] + [(255, 32 * k, 0) for k in range(2)]
    assert rv.mosaic_colours(*picture(scheme)) < rv.MOSAIC_COLOURS
    assert rv.mosaic_colours(*picture(mosaic)) >= rv.MOSAIC_COLOURS
    assert rv.mosaic_colours(*picture([(255, 255, 255)])) == 0


def test_keys_reach_the_item_from_any_current_one():
    keys = rv.keys_to(6, 3)
    assert keys[:8] == [rv.VK_UP] * 8  # past the top: Home does not work in LIRA's list
    assert keys[8:] == [rv.VK_DOWN] * 3 + [rv.VK_RETURN]


@pytest.mark.parametrize(
    ("spec", "parsed"),
    [
        ("N:MAXIMUM", ("N", "rsn", "MAXIMUM")),
        ("Rz:MAX ABS", ("Rz", "rsn", "MAX ABS")),
        ("disp_Z:rsn", ("disp_Z", "rsn", "rsn")),
        ("My:rsn5", ("My", "rsn", 5)),
        ("Qz:case3", ("Qz", "load_cases", 3)),
        ("steel_1LS", ("steel_1LS", "load_cases", None)),
    ],
)
def test_picture_specs(spec, parsed):
    assert rp.parse_spec(spec) == parsed


@pytest.mark.parametrize("spec", ["N", "N:max", "Nx:MAXIMUM", "steel_LB:rsn2"])
def test_wrong_specs_are_refused(spec):
    with pytest.raises(LiraInputError):
        rp.parse_spec(spec)


def ankara(**group):
    return {
        "model": "a.lir",
        "title": "Здание 1",
        "scheme_appendix": "В",
        "appendix": "Ж",
        "groups": [{"title": "Усилия в арке", "stiffnesses": [1], "view": "xoz"} | group],
    }


def test_the_default_plan_is_that_of_the_arch_in_the_sample():
    pictures = rp.plan([ankara()])
    scheme = [p for p in pictures if p.appendix == "В"]
    assert [p.view for p in scheme] == ["isometric", "xoy", "xoz"]
    assert all(p.results is None and p.group is None for p in scheme)
    arch = [p for p in pictures if p.appendix == "Ж"]
    assert [(p.mosaic, p.item) for p in arch] == [
        ("N", "MAXIMUM"),
        ("N", "MINIMUM"),
        ("steel_1LS", None),
        ("steel_LB", None),
        ("disp_Z", "rsn"),
    ]
    assert {p.view for p in arch} == {"xoz"}
    foundation = rp.plan([ankara(foundation=True)])
    assert [(p.mosaic, p.item) for p in foundation if p.appendix == "Ж"] == [
        ("Rz", "MINIMUM"),
        ("Rz", "MAXIMUM"),
        ("Rz", "rsn"),
        ("disp_Z", "rsn"),
    ]


@pytest.mark.parametrize(
    ("models", "words"),
    [
        ([], "at least one"),
        ([{"title": "x"}], "model"),
        ([{"model": "a.lir", "groups": [{"title": "g", "elements": [1]}]}], "appendix"),
        ([ankara(stiffnesses=None)], "stiffnesses, elements or whole_model"),
        ([ankara(view="top")], "view"),
        ([ankara(pictures=["Nx:MAXIMUM"])], "Unknown picture"),
    ],
)
def test_wrong_descriptions_are_refused(models, words):
    with pytest.raises(LiraInputError, match=words):
        rp.plan(models)


def frame(adapter, tmp_path):
    """Four bars: 1, 2 of stiffness 1 (nodes 1-3), 3 of stiffness 2, 4 of stiffness 3."""
    path = str(tmp_path / "a.lir")
    adapter.docs[path] = {
        "title": "a",
        "path": path,
        "type": 5,
        "modified": False,
        "tables": {
            "Elements_Stiffnesses": [[1, 1], [2, 1], [3, 2], [4, 3]],
            "Elements_TypeAndNodes": [
                [1, 10, "1, 2"],
                [2, 10, "2, 3"],
                [3, 10, "3, 4"],
                [4, 10, "4, 5"],
            ],
        },
    }
    adapter.active = path

    def displacements(model_path, nodes, combinations):
        z = {(2, 1): -0.004, (2, 2): -0.009, (4, 3): -0.050}  # RSN 3 moves another group
        return [
            {"node": n, "combination": c, "Z": z.get((n, c), 0.0)}
            for n in nodes or [1, 2, 3, 4, 5]
            for c in (1, 2, 3)
        ]

    adapter.combination_displacements = displacements
    return path


async def test_the_tool_takes_each_picture_and_names_the_worst_rsn(tmp_path):
    session, adapter = fake_session(tmp_path)
    path = frame(adapter, tmp_path)
    out = tmp_path / "pictures"
    model = ankara() | {"model": path}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_rpz_pictures", {"output_dir": str(out), "models": [model]}
        )
    assert not res.is_error, res.content[0].text
    shown = adapter.shown_pictures
    assert shown[0] == (None, None, None, None, "isometric")
    assert shown[3] == ("rsn", "MAXIMUM", "N", [1, 2], "xoz")  # stiffness 1: elements 1, 2
    assert shown[5] == ("load_cases", None, "steel_1LS", [1, 2], "xoz")
    assert shown[7] == ("rsn", 2, "disp_Z", [1, 2], "xoz")  # RSN 2: 9 mm in the arch
    data = res.structured_content
    names = [p["file"] for p in data["pictures"]]
    assert names[0] == "В01 Здание 1 - схема isometric.png"
    assert names[3] == "Ж01 Усилия в арке - N огибающая max.png"
    assert names[7] == "Ж05 Усилия в арке - перемещения Z 2. РСН2.png"
    assert data["pictures"][7]["notes"] == ["РСН 2: наибольшее |Z| в группе 9,0 мм"]
    assert all((out / n).stat().st_size > 0 for n in names)
    index = json.loads((out / "pictures.json").read_text(encoding="utf-8"))
    assert index[3]["mosaic"] == "N" and index[3]["item"] == "MAXIMUM"
    assert "elements" not in index[3]


async def test_a_picture_without_the_mosaic_is_kept_with_a_note(tmp_path):
    session, adapter = fake_session(tmp_path)
    path = frame(adapter, tmp_path)
    model = ankara(pictures=["N:MAX ABS"]) | {"model": path, "scheme_appendix": None}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_rpz_pictures", {"output_dir": str(tmp_path / "p"), "models": [model]}
        )
    assert not res.is_error, res.content[0].text
    (picture,) = res.structured_content["pictures"]
    assert "Мозаики на картинке не видно" in picture["notes"][0]


async def test_a_stiffness_with_no_elements_is_an_error_not_an_empty_picture(tmp_path):
    session, adapter = fake_session(tmp_path)
    path = frame(adapter, tmp_path)
    model = ankara(stiffnesses=[9]) | {"model": path, "scheme_appendix": None}
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_rpz_pictures", {"output_dir": str(tmp_path / "p"), "models": [model]}
        )
    assert res.is_error and "No elements of stiffnesses [9]" in res.content[0].text
    assert not getattr(adapter, "shown_pictures", [])


class Window:
    """LIRA's view as the adapter captures it: a mosaic command toggles the mosaic."""

    def __init__(self, shown: bool, has_result: bool = True):
        self.shown, self.has_result, self.commands = shown, has_result, []

    def StartCmd(self, cmd):  # LIRA's name
        self.commands.append(cmd)
        if self.has_result:
            self.shown = not self.shown

    def picture(self, ref, view):
        colours = [(0, 32 * k, 255) for k in range(8)] if self.shown else [(0, 160, 0)]
        rows = []
        for k in range(80):
            b, g, r = colours[k % len(colours)]
            rows += [b, g, r, 255] * 50
        return 50, 80, bytes(rows)


def adapter_on(window):
    from lira_sapr_mcp.adapters.com_adapter import ComLiraAdapter

    a = ComLiraAdapter.__new__(ComLiraAdapter)
    a._app, a._settle, a.model_picture = window, lambda s: None, window.picture
    return a


@pytest.mark.parametrize(("shown", "presses"), [(False, 1), (True, 2)])
def test_the_mosaic_is_asked_again_when_lira_hid_it(shown, presses):
    window = Window(shown)
    *_, drawn = adapter_on(window)._draw_mosaic("m", "xoz", "N")
    assert drawn and window.commands == [rv.MOSAICS["N"][0]] * presses


def test_a_mosaic_lira_has_nothing_for_is_not_taken_for_the_old_one():
    window = Window(shown=True, has_result=False)  # N still drawn, no steel check
    *_, drawn = adapter_on(window)._draw_mosaic("m", "xoz", "steel_1LS")
    assert not drawn and len(window.commands) == 3
