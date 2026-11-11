"""RPZ pictures from LIRA's window (lira_rpz_pictures) on test copies of tutorial models:
LIRA shows the asked item of its list and draws the mosaic, a mosaic asked twice in a row
is not lost, the steel check, and a result not drawn is noted instead of shown as the old one."""

import json
from pathlib import Path

import pytest
from mcp import Client
from test_live_k2 import analysed, call, tutorial

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

NOT_DRAWN = "Мозаики на картинке не видно"


def pictures(workdir: Path) -> list[dict]:
    return json.loads((workdir / "pictures" / "pictures.json").read_text(encoding="utf-8"))


async def test_envelopes_one_after_another_and_the_steel_of_example_04(mcp, workdir):
    """Example 04: N MAXIMUM, N MINIMUM, N MAXIMUM again (the same mosaic command hides
    LIRA's mosaic), the worst RSN by |Z|, and the steel check: stiffness 1 of example 04
    has steel sections (LIRA draws 1st LS 32.8 to 100 %, 2026-11-11)."""
    path = tutorial("04", workdir, "lmcp_pictures_04")
    model = {
        "model": path,
        "title": "Пример 04",
        "scheme_appendix": "В",
        "scheme_views": ["isometric"],
        "appendix": "Ж",
        "groups": [
            {
                "title": "Жесткость 1",
                "stiffnesses": [1],
                "view": "isometric",
                "pictures": ["N:MAXIMUM", "N:MINIMUM", "N:MAXIMUM", "disp_Z:rsn", "steel_1LS"],
            }
        ],
    }
    async with Client(mcp) as client:
        await call(client, "lira_open_model", {"path": path})
        await analysed(client, path)
        out = await call(
            client,
            "lira_rpz_pictures",
            {"output_dir": str(workdir / "pictures"), "models": [model]},
        )
    shown = [(p["shown"], p.get("notes", [])) for p in out["pictures"]]
    assert shown[0] == ("", [])  # the scheme
    assert shown[1:4] == [("MAXIMUM", []), ("MINIMUM", []), ("MAXIMUM", [])]
    item, notes = shown[4]
    assert item.endswith(f"РСН{item.split('.')[0]}") and notes[0].startswith("РСН ")
    assert shown[5] == ("", [])  # the steel check, drawn
    assert all(Path(f).stat().st_size > 5_000 for f in out["files"])
    assert [p["file"][:3] for p in pictures(workdir)] == ["В01", "Ж01", "Ж02", "Ж03", "Ж04", "Ж05"]


async def test_the_steel_check_of_a_steel_frame_is_drawn(mcp, workdir):
    """Example 12 (steel frame): steel mosaics per element (assigned and selected
    sections) and by design sections, each after another mosaic, then N of load case 1
    (the example has no RSN)."""
    path = tutorial("12", workdir, "lmcp_pictures_12")
    steel = [
        "steel_1LS",
        "steel_2LS",
        "steel_LB",
        "steel_max",
        "steel_sel_1LS",
        "steel_sel_max",
        "steel_sec_normal",
        "steel_sel_sec_normal",
        "steel_sec_stab_2planes",
    ]
    model = {
        "model": path,
        "appendix": "К",
        "groups": [{"title": "Каркас", "whole_model": True, "pictures": [*steel, "N:case1"]}],
    }
    async with Client(mcp) as client:
        await call(client, "lira_open_model", {"path": path})
        await analysed(client, path)
        out = await call(
            client,
            "lira_rpz_pictures",
            {"output_dir": str(workdir / "pictures"), "models": [model]},
        )
    assert [p.get("notes", []) for p in out["pictures"]] == [[]] * (len(steel) + 1)
