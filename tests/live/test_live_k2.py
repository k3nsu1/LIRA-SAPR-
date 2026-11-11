"""K2 on live LIRA, only on test copies of installed tutorial models: what DCL holds, how
LIRA composes an RSN by limit state, and steel check percentages from the .rez file."""

import shutil
from pathlib import Path

import pytest
from mcp import Client

from lira_sapr_mcp.postprocessing import steel

pytestmark = [pytest.mark.lira, pytest.mark.anyio]

MANUAL = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")


def tutorial(number: str, workdir: Path, name: str) -> str:
    sources = sorted(MANUAL.glob(f"{number}*/*.lir"))
    if not sources:
        pytest.skip(f"tutorial {number} is not installed")
    path = workdir / f"{name}.lir"
    shutil.copyfile(sources[0], path)
    return str(path)


async def analysed(client: Client, path: str) -> None:
    res = await client.call_tool("lira_run_analysis", {"model": path, "timeout_s": 300})
    assert not res.is_error and res.structured_content["status"] == "finished", res.content


async def call(client: Client, tool: str, args: dict) -> dict:
    res = await client.call_tool(tool, args)
    assert not res.is_error, res.content[0].text
    return res.structured_content


async def test_dcl_gives_gamma_f_and_share_and_the_rsn_follows_them(mcp, workdir):
    """Example 04: RSN1 = L1 + L2 + L3 at the 1st limit state; at the 2nd LIRA divides L2 by
    1.1 and L3 by 1.4 (γf of DCL), long-term parts take 0.35 of L3 (its share)."""
    path = tutorial("04", workdir, "lmcp_k2_dcl")
    async with Client(mcp) as client:
        await call(client, "lira_open_model", {"path": path})
        dcl = await call(client, "lira_read_table", {"table": "DCL", "model": path})
        rows = {r[0]: r for r in dcl["rows"] if r[0]}
        assert [rows[n][6] for n in ("1", "2", "3")] == ["1.1", "1.1", "1.4"]
        assert [rows[n][7] for n in ("1", "2", "3")] == ["1", "1", "0.35"]
        await analysed(client, path)
        facts = await call(client, "lira_rpz_facts", {"model": path})
        cases = {c["number"]: c for c in facts["load_cases"]}
        assert (cases[3]["gamma_f"], cases[3]["long_term_share"]) == (1.4, 0.35)
        (rsn1,) = [c for c in facts["combinations"] if c["number"] == 1]
        assert rsn1["exact"] and {m["case"]: m["multiplier"] for m in rsn1["make_up"]} == {
            1: 1,
            2: 1,
            3: 1,
        }
        args = {"model": path, "elements": [105], "families": ["bar"], "components": ["N"]}
        by_case = await call(client, "lira_element_forces", args | {"load_cases": [1, 2, 3]})
        by_rsn = await call(client, "lira_combination_forces", args | {"combinations": [1]})
    n = {r["case"]: r["N"] for r in by_case["families"]["bar"]["rows"] if r["section"] == 1}
    rsn = {r["limit_state"]: r["N"] for r in by_rsn["families"]["bar"]["rows"] if r["section"] == 1}
    assert rsn["ultimate"] == pytest.approx(n[1] + n[2] + n[3], rel=1e-5)
    assert rsn["ultimate_long"] == pytest.approx(n[1] + n[2] + 0.35 * n[3], rel=1e-5)
    # Load case 1 enters the 2nd limit state undivided, though DCL gives it γf 1.1 like
    # case 2: recorded as found (docs/research/lira-api.md, K2), the reason is the owner's.
    assert rsn["serviceability"] == pytest.approx(n[1] + n[2] / 1.1 + n[3] / 1.4, rel=1e-5)


async def test_steel_check_reads_what_lira_wrote(mcp, workdir):
    """Example 12 (steel frame): the analysis writes <model>.rez into LIRA's work folder;
    the tool returns its percentages character for character."""
    path = tutorial("12", workdir, "lmcp_k2_steel")
    async with Client(mcp) as client:
        await call(client, "lira_open_model", {"path": path})
        await analysed(client, path)
        await call(client, "lira_open_model", {"path": path})
        out = await call(client, "lira_steel_check", {"model": path, "top": None})
    rez = steel.load(out["rez"])
    assert Path(out["rez"]).name == "lmcp_k2_steel.rez"
    assert out["elements_checked"] == len(rez.elements) > 0
    for row in out["rows"]:
        assert [row[m] for m in steel.MEASURES] == rez.elements[row["element"]].asn_pct
    over = {o["element"] for o in out["over_100"]}
    assert over == {
        n
        for n, e in rez.elements.items()
        if any(t not in ("NR",) and float(t) < steel.SERVICE_VALUE and float(t) > 100
               for t in e.asn_pct)
    }  # fmt: skip
    # The .rez names this model (K2 review: a model of the same name elsewhere writes the
    # same file in the shared work folder): no origin warning on the model's own check.
    assert "names the model" not in out.get("warning", ""), rez.general.get("Title")
