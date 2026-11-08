"""Snow and wind loads of Kazakhstan: the same numbers as the owner's «ЛИРА+ Записка».

The reference values come from running the app's own JavaScript (its calculations "по нормам"
and the duo-pitched sheet) on these inputs; defaults are the app's (Ce "Обычные", Ct = 1,
cdir = cseason = 1, ρ = 1.25, γ snow = 2, roof "Свес крыши с острыми углами")."""

import json
from pathlib import Path

import pytest

from lira_sapr_mcp.postprocessing import loads_kz as lk

REFERENCE = json.loads(
    (Path(__file__).parent / "loads_kz_reference.json").read_text(encoding="utf-8")
)
ROOF_DEFAULT = "Свес крыши с острыми углами"


def compute(case):
    i = case["inputs"]
    if case["id"] == "windN":
        c = lk.climate(i["wn_city"])
        return lk.wind_building(
            c, i["wn_terr"], i["wn_h"], i["wn_l1"], i["wn_l2"], i.get("wn_roof", ROOF_DEFAULT)
        )
    if case["id"] == "bagN":
        c = lk.climate(i["bn_city"])
        return lk.snow_step(
            c, i["bn_a1"], i["bn_aup"], i["bn_h"], i["bn_b1"], i["bn_b2"], i["bn_bup"]
        )
    if case["id"] == "multiN":
        c = lk.climate(i["mn_city"])
        return lk.snow_multispan(
            c,
            i["mn_L"],
            i["mn_a1"],
            i["mn_a2"],
            i["mn_a3"],
            i["mn_a4"],
            i["mn_l2"],
            i["mn_l3"],
            i["mn_h"],
        )
    if case["id"] == "duo":
        c = lk.climate(i["duo_city"])
        return lk.snow_duopitch(c, i["duo_L"], i["duo_a1"], i["duo_a2"])
    if case["id"] == "arch":
        c = lk.climate(i["ar_city"])
        return lk.wind_vault(c, i["ar_terr"], i["ar_h"], i["ar_f"], i["ar_d"], i["ar_l"])
    raise AssertionError(case["id"])


@pytest.mark.parametrize(
    "case", REFERENCE["cases"], ids=lambda c: f"{c['id']}-{next(iter(c['inputs'].values()))}"
)
def test_the_same_numbers_as_the_owner_app(case):
    got = compute(case).values
    for key, value in case["keys"].items():
        if key in ("district",):
            continue
        assert got[key] == pytest.approx(value, abs=1e-9), key


def test_karaganda_wind_matches_the_sample_review():
    # «Разбор образцов», remark 3: h = 7.8 m, terrain III -> qp ≈ 0.56 kPa (57 kg/m²)
    r = lk.wind_building(lk.climate("Караганда"), "III", 7.8, 72, 41.5)
    assert r.values["qp"] == pytest.approx(56.6)
    assert any("zmin = 5 м" in line for line in r.lines)


def test_excel_rounding_and_russian_numbers():
    assert lk.rnd(2.675, 2) == 2.68 and lk.rnd(-0.5, 0) == -1
    assert lk.fmt(1.5, "кПа") == "1,5 кПа" and lk.fmt(float("nan")) == "#Н/Д"
    assert lk.mu1(30) == 0.8 and lk.mu1(45) == 0.4 and lk.mu1(60) == 0


def test_unknown_city_and_missing_wind_district_are_said():
    with pytest.raises(ValueError, match="не в справочнике"):
        lk.climate("Атырау")
    with pytest.raises(ValueError, match="не задан ветровой район"):
        lk.wind_building(lk.climate("Петропавловск"), "II", 6, 30, 12)


@pytest.mark.anyio
async def test_the_tool_answers_and_writes_section_3(tmp_path):
    from mcp import Client

    from fakes import fake_session
    from lira_sapr_mcp.server import build_server

    session, _ = fake_session(tmp_path)
    calcs = [
        {"kind": "snow_duopitch", "span": 27, "a1": 20, "a2": 40},
        {"kind": "wind_building", "terrain": "III", "h": 7.8, "l1": 72, "l2": 41.5},
    ]
    async with Client(build_server(session)) as client:
        res = await client.call_tool(
            "lira_kz_loads",
            {
                "city": "Караганда",
                "calculations": calcs,
                "output_dir": str(tmp_path),
                "name": "Навес",
            },
        )
        other = await client.call_tool(
            "lira_kz_loads",
            {
                "districts": {"wind": "III", "snow4": "IV", "snow5": "IV", "snow6": "II"},
                "city": "Атырау",
                "calculations": [{"kind": "snow_duopitch", "span": 12, "a1": 10, "a2": 10}],
            },
        )
        bad = await client.call_tool(
            "lira_kz_loads",
            {"city": "Караганда", "calculations": [{"kind": "snow_duopitch", "span": 12}]},
        )
        nowhere = await client.call_tool("lira_kz_loads", {"calculations": calcs})
    assert not res.is_error, res.content[0].text
    snow, wind = res.structured_content["results"]
    assert snow["values"]["snowRoof"] == 122.3 and wind["values"]["qp"] == 56.6
    assert (tmp_path / "Навес_нагрузки.docx").stat().st_size > 0
    assert other.structured_content["results"][0]["values"]["sk"] == 1.8
    assert "Атырау" in other.structured_content["results"][0]["lines"][0]
    assert bad.is_error and "нет a1, a2" in bad.content[0].text
    assert nowhere.is_error and "city or districts" in nowhere.content[0].text
