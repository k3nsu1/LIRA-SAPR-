"""lira_steel_check on the fake adapter with synthetic .rez files."""

import os

import pytest
from mcp import Client
from openpyxl import load_workbook

from fakes import fake_session
from lira_sapr_mcp.server import build_server

pytestmark = pytest.mark.anyio

REZ = """[General]
Title=w
VarSetName=Вариант 1
BuildingCodes=SP
ForcesSrc=DCF
ResultMode=PercentLS
[Selected Sections]
1 =  Section = DoubleT  Shape = |18Б2|
[Assigned Sections]
1 =  Section = DoubleT  Name = |1.1.1.  Двутавр 30Б1|  Shape = |30Б1|
2 =  Section = DoubleT  Name = |2.1.2.  Двутавр 35К1|  Shape = |35К1|
3 =  Section = Angle  Name = |Уголок 75x6|
[Elements]
;ELEM  SECTIONS   UTILIZATION
1 = 1,1, 50.0,10,10, 63.10,12.6,45.8, 0,NR
2 = 1,1, 50.0,10,10, 100.30,12.6,45.8, 0,NR
3 = 1,1, 50.0,10,10, 100.30,99.9,NR, 0,NR
4 = 1,2, 50.0,10,10, 80.0,101.5,100000000000000000000, 0,NR
5 = NR,3, NR,NR,NR, NR,NR,NR, 0,NR
6 = 1,3, 50.0,10,10, 7.25,1.0,2.0, 0,NR
"""


def setup(tmp_path, adapter, name="w.rez", text=REZ):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    model = tmp_path / "w.lir"
    model.write_bytes(b"lir")
    rez = work / name
    rez.write_bytes(text.encode("cp1251"))
    stamp = model.stat().st_mtime + 10
    os.utime(rez, (stamp, stamp))  # the check is newer than the model
    adapter.work_dir = str(work)
    adapter.docs[str(model)] = {
        "title": "w",
        "path": str(model),
        "type": 5,
        "modified": False,
        "tables": {},
    }
    adapter.active = str(model)
    return model, rez


async def call(session, args):
    async with Client(build_server(session)) as client:
        return await client.call_tool("lira_steel_check", args)


async def test_percentages_are_lira_strings_with_groups_and_the_100_percent_fact(tmp_path):
    session, adapter = fake_session(tmp_path)
    setup(tmp_path, adapter)
    res = await call(session, {"top": None})
    assert not res.is_error, res.content[0].text
    out = res.structured_content
    assert out["general"]["VarSetName"] == "Вариант 1" and out["encoding"] == "windows-1251"
    rows = {r["element"]: r for r in out["rows"]}
    # Copied as written: trailing zeros kept, the service value 1e20 and NR stay strings
    assert rows[1]["1LS"] == "63.10" and rows[5]["1LS"] == "NR"
    assert rows[4]["LB"] == "100000000000000000000"
    assert rows[2]["section_name"] == "Двутавр 30Б1" and rows[2]["stiffness"] == 1
    assert out["over_100_count"] == 3
    assert [(o["element"], o["measures"]) for o in out["over_100"]] == [
        (2, ["1LS"]),
        (3, ["1LS"]),
        (4, ["2LS"]),
    ]
    assert rows[3]["over_100"] == ["1LS"] and rows[1]["over_100"] == []
    # Sorted by the largest of the three, no result last
    assert [r["element"] for r in out["rows"]] == [4, 2, 3, 1, 6, 5]
    groups = {(g.get("stiffness"), g.get("section")): g for g in out["groups"]}
    one = groups[(1, None)]
    # Ties keep the smaller element, as LiraRez does
    assert one["1LS"] == {"max": "100.30", "element": 2, "no_result": 0}
    assert one["LB"] == {"max": "45.8", "element": 1, "no_result": 1}
    assert groups[(2, None)]["LB"] == {"max": None, "element": None, "no_result": 1}
    angle = groups[(None, 3)]
    assert angle["1LS"] == {"max": "7.25", "element": 6, "no_result": 1}


async def test_stiffness_and_element_filters_are_a_union_with_warnings(tmp_path):
    session, adapter = fake_session(tmp_path)
    setup(tmp_path, adapter)
    res = await call(session, {"stiffnesses": [2, 9], "elements": [6]})
    assert not res.is_error, res.content[0].text
    out = res.structured_content
    assert sorted(r["element"] for r in out["rows"]) == [4, 6]
    assert "жёсткость 9 не найдена среди назначенных сечений" in out["warnings"]


async def test_design_option_two_reads_the_hash_file_and_a_stale_file_is_flagged(tmp_path):
    session, adapter = fake_session(tmp_path)
    model, rez = setup(tmp_path, adapter, name="w#1.rez")
    stamp = model.stat().st_mtime - 100
    os.utime(rez, (stamp, stamp))
    res = await call(session, {"design_option": 2})
    assert not res.is_error, res.content[0].text
    assert res.structured_content["rez"].endswith("w#1.rez")
    assert "older than w.lir" in res.structured_content["warning"]
    missing = await call(session, {})
    assert missing.is_error and "No w.rez" in missing.content[0].text
    assert "found: w#1.rez" in missing.content[0].text


async def test_bad_files_and_arguments_are_refused(tmp_path):
    session, adapter = fake_session(tmp_path)
    setup(tmp_path, adapter, text=REZ.replace("PercentLS", "Sections"))
    bad = await call(session, {})
    assert bad.is_error and "ResultMode должен быть PercentLS" in bad.content[0].text
    relative = await call(session, {"rez_path": "w.rez"})
    assert relative.is_error and "absolute" in relative.content[0].text
    absent = await call(session, {"rez_path": str(tmp_path / "none.rez")})
    assert absent.is_error and "Нет файла результатов ЛИРЫ" in absent.content[0].text
    sort = await call(session, {"rez_path": str(tmp_path / "work" / "w.rez"), "sort_by": "N"})
    assert sort.is_error


async def test_export_writes_elements_and_groups(tmp_path):
    session, adapter = fake_session(tmp_path)
    setup(tmp_path, adapter)
    target = tmp_path / "steel.xlsx"
    res = await call(session, {"top": 1, "export_path": str(target)})
    assert not res.is_error, res.content[0].text
    assert res.structured_content["rows_shown"] == 1
    book = load_workbook(target)
    assert book.sheetnames == ["elements", "groups"]
    head = [c.value for c in book["elements"][1]]
    assert head[:4] == ["element", "section", "section_name", "stiffness"]
    assert book["elements"].max_row == 7  # header and all six elements, not only the top
    values = {row[0].value: row[head.index("1LS")].value for row in book["elements"].iter_rows(2)}
    assert values[1] == "63.10"
