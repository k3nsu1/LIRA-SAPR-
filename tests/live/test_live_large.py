"""Acceptance example 4: the 10 largest forces of a model with 10 000+ elements, one request.

A space frame of 22 x 22 x 9 nodes generated in LIRA's text format: 11 264 bars, fixed
bases, 1 t/m on every beam.
"""

import time

import pytest
from mcp import Client

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

NX, NY, NZ = 22, 22, 9  # nodes along X, Y and Z; 3 m spans


def space_frame() -> tuple[str, int]:
    def node(i, j, k):
        return 1 + i + NX * (j + NY * k)

    coords = [f"{3 * i} {3 * j} {3 * k}" for k in range(NZ) for j in range(NY) for i in range(NX)]
    beams, columns = [], []
    for k in range(NZ):
        for j in range(NY):
            for i in range(NX):
                if k and i + 1 < NX:
                    beams.append((node(i, j, k), node(i + 1, j, k)))
                if k and j + 1 < NY:
                    beams.append((node(i, j, k), node(i, j + 1, k)))
                if k + 1 < NZ:
                    columns.append((node(i, j, k), node(i, j, k + 1)))
    bars = beams + columns
    fixed = [node(i, j, 0) for j in range(NY) for i in range(NX)]
    text = "\n".join(
        [
            "( 0/ 1; LARGE/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )",
            "( 1/ " + " ".join(f"10 1 {a} {b}/" for a, b in bars) + " )",
            "( 3/ 1 S0 3E6 40 60/ 0 RO 2.5/ )",
            "( 4/ " + " ".join(f"{c}/" for c in coords) + " )",
            "( 5/ " + " ".join(f"{n} 1 2 3 4 5 6/" for n in fixed) + " )",
            "( 6/ " + " ".join(f"{e} 6 3 1 1/" for e in range(1, len(beams) + 1)) + " )",
            "( 7/ 1 1/ )",
        ]
    )
    return text + "\n", len(bars)


async def test_ten_largest_moments_of_a_large_model(mcp, workdir):
    text, count = space_frame()
    assert count > 10_000
    path = str(workdir / "lmcp_large.lir")
    async with Client(mcp) as client:
        made = await client.call_tool("lira_import_text_model", {"text": text, "save_as": path})
        assert not made.is_error, made.content
        summary = await client.call_tool("lira_model_summary", {})
        res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 600})
        assert not res.is_error and res.structured_content["status"] == "finished", res.content
        started = time.monotonic()
        top = await client.call_tool(
            "lira_element_forces", {"model": path, "sort_by": "My", "top": 10}
        )
        took = time.monotonic() - started
    assert summary.structured_content["elements"] == count
    assert not top.is_error, top.content
    bar = top.structured_content["families"]["bar"]
    assert bar["total_rows"] == 2 * count  # two design sections per bar
    assert bar["rows_shown"] == 10
    values = [abs(r["My"]) for r in bar["rows"]]
    assert values == sorted(values, reverse=True)
    assert values[0] == pytest.approx(
        max(abs(bar["extremes"]["My"]["min"]["value"]), bar["extremes"]["My"]["max"]["value"])
    )
    print(f"example 4: {count} bars, top 10 My in {took:.1f} s")
    assert took < 120
