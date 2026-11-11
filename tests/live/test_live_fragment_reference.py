"""Reference cantilever for confirming fragment force and moment signs in the LIRA UI."""

import pytest
from mcp import Client
from test_live_k2 import analysed, call
from test_live_loads import FRAME
from test_live_results import in_tons

pytestmark = [pytest.mark.lira, pytest.mark.anyio]


async def test_oblique_cantilever_has_known_base_resultant(mcp, workdir):
    # Same bar section, units and vertical line load as the verified tutorial frame.
    # Beam from (0,0,0) to (4,3,0), length 5 m; only node 1 fixed, 10 tf/m down.
    text = (
        FRAME.replace("10 1 1 3/ 10 1 2 4/ 10 1 3 4/", "10 1 1 2/")
        .replace("0 0 0/ 6 0 0/ 0 0 3/ 6 0 3/", "0 0 0/ 4 3 0/")
        .replace(" 2 1 2 3 4 5 6/", "")
        .replace("3 6 3 1 1/", "1 6 3 1 1/")
    )
    path = str(workdir / "lmcp_k2_sign_ref.lir")
    async with Client(mcp) as client:
        await call(client, "lira_import_text_model", {"text": text, "save_as": path})
        await analysed(client, path)
        out = await call(
            client,
            "lira_element_forces",
            {
                "model": path,
                "elements": [1],
                "load_cases": [1],
                "components": ["Qz", "My"],
                "top": None,
            },
        )
        bar = out["families"]["bar"]
        root = next(row for row in bar["rows"] if row["section"] == 1)
        assert abs(in_tons(root["Qz"], bar["units"]["Qz"])) == pytest.approx(50, abs=0.002)
        assert abs(in_tons(root["My"], bar["units"]["My"])) == pytest.approx(125, abs=0.002)
        disp = await call(
            client,
            "lira_node_displacements",
            {"model": path, "nodes": [2], "load_cases": [1], "components": ["Z"]},
        )
        assert disp["rows"][0]["Z"] < 0
