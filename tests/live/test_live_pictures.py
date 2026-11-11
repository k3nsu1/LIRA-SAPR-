"""RPZ pictures from LIRA's window (lira_rpz_pictures) on test copies of tutorial models:
LIRA shows the asked item of its list and draws the mosaic, a mosaic asked twice in a row
is not lost, and a result the model has not got is noted instead of shown as the old one."""

import json
from pathlib import Path

import pytest
from mcp import Client
from test_live_k2 import analysed, call, tutorial

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

NOT_DRAWN = "Мозаики на картинке не видно"


def pictures(workdir: Path) -> list[dict]:
    return json.loads((workdir / "pictures" / "pictures.json").read_text(encoding="utf-8"))


async def test_envelopes_one_after_another_and_no_steel_in_a_concrete_frame(mcp, workdir):
    """Example 04: N MAXIMUM, N MINIMUM, N MAXIMUM again (the same mosaic command hides
    LIRA's mosaic), the worst RSN by |Z|, and the steel check the model has not got."""
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
    shown = [(p["shown"], p["notes"]) for p in out["pictures"]]
    assert shown[0] == ("", [])  # the scheme
    assert shown[1:4] == [("MAXIMUM", []), ("MINIMUM", []), ("MAXIMUM", [])]
    item, notes = shown[4]
    assert item.endswith(f"РСН{item.split('.')[0]}") and notes[0].startswith("РСН ")
    assert NOT_DRAWN in shown[5][1][0]
    assert all(Path(f).stat().st_size > 5_000 for f in out["files"])
    assert [p["file"][:3] for p in pictures(workdir)] == ["В01", "Ж01", "Ж02", "Ж03", "Ж04", "Ж05"]


@pytest.mark.xfail(
    strict=True,
    reason="2026-11-11: commands 5592/5596 change nothing in the results mode, nor after "
    "11972 (design mode) or 3804 (steel results); how LIRA shows the steel check is open",
)
async def test_the_steel_check_of_a_steel_frame_is_drawn(mcp, workdir):
    """Example 12 (steel frame): the 1st limit state and local buckling mosaics."""
    path = tutorial("12", workdir, "lmcp_pictures_12")
    model = {
        "model": path,
        "appendix": "К",
        "groups": [
            {
                "title": "Каркас",
                "whole_model": True,
                "pictures": ["steel_1LS", "steel_LB", "N:MAXIMUM"],
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
    assert [p["notes"] for p in out["pictures"]] == [[], [], []]
