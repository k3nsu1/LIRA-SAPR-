"""Check design station coordinates against beam equilibrium on a test frame."""

from pathlib import Path

import pytest
from mcp import Client
from test_live_k2 import analysed, call
from test_live_loads import FRAME
from test_live_results import in_tons

pytestmark = [pytest.mark.lira, pytest.mark.anyio]


@pytest.mark.parametrize("count", [5, 7])
async def test_diagram_stations_match_beam_equilibrium(mcp, workdir, count):
    path = str(workdir / f"lmcp_k2_stations_{count}.lir")
    async with Client(mcp) as client:
        await call(client, "lira_import_text_model", {"text": FRAME, "save_as": path})
        await call(
            client,
            "lira_write_table",
            {"table": "Elements_DesignSectionsCount", "rows": [[3, count]], "model": path},
        )
        await call(client, "lira_save_model", {"model": path})
        await call(client, "lira_close_model", {"model": path})
        await call(client, "lira_open_model", {"path": path})
        await analysed(client, path)
        out = await call(
            client,
            "lira_element_forces",
            {"model": path, "elements": [3], "load_cases": [1], "components": ["My"], "top": None},
        )
        bar = out["families"]["bar"]
        moments = {r["section"]: in_tons(r["My"], bar["units"]["My"]) for r in bar["rows"]}
        assert len(moments) == count
        for section, moment in moments.items():
            t = (section - 1) / (count - 1)
            x = 6 * t
            expected = (1 - t) * moments[1] + t * moments[count] + 10 * x * (6 - x) / 2
            assert moment == pytest.approx(expected, abs=0.002)
        diagrams = await call(
            client,
            "lira_force_diagrams",
            {
                "model": path,
                "elements": [3],
                "load_case": 1,
                "output_dir": str(workdir / "pictures"),
                "view": "xz",
            },
        )
        assert len(diagrams["files"]) == 4
        assert all(Path(p).stat().st_size > 10000 for p in diagrams["files"])
