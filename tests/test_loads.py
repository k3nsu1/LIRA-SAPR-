"""Changing loads by rebuilding the model from text, on the in-memory adapter."""

import pytest

from fakes import FakeAdapter
from lira_sapr_mcp.adapters.base import TableError
from lira_sapr_mcp.errors import LiraError, LiraInputError
from lira_sapr_mcp.services.loads import (
    Targets,
    _keep_zero_entries,
    _loads_problem,
    _same,
    _same_loads,
    read_loads,
    set_loads,
)
from lira_sapr_mcp.services.textformat import Load, TextModel

FRAME = """( 0/ 1; FRAME/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )
( 1/ 10 1 1 3/ 10 1 2 4/ 10 1 3 4/ )
( 4/ 0 0 0/ 6 0 0/ 0 0 3/ 6 0 3/ )
( 6/ 3 6 3 1 1/ 4 0 1 2 2/ )
( 7/ 1 10/ 2 5/ )
"""
TASK = "1. Основная задача"
CASES = [[1, "Dead", TASK, "Постоянное(П)", None], [2, "Wind", TASK, "Мгновенное(М)", None]]
# Kinds as LIRA's default norms (SP 20.13330) name them, and what LIRA turns a SNiP
# 2.01.07-85* kind into when it is written into a model with those norms.
SP20 = {"Постоянное ( P)", "Кратк. доминир.1 ( Pt1)", "Мгновенное(М)", "Неактивное (Н/а)"}
SNIP_IN_SP20 = {"Постоянное(П)": "Постоянное ( P)", "Длительное(Д)": "Постоянное ( P)"}


@pytest.fixture
def model(tmp_path):
    adapter = FakeAdapter()
    path = tmp_path / "frame.lir"
    path.write_bytes(b"original")
    adapter.open_document(str(path))
    doc = adapter.docs[str(path)]
    doc["text"], doc["tables"]["LoadCases"] = FRAME, [list(r) for r in CASES]
    return adapter, path


def import_resets_kinds(adapter, kinds, cases=CASES):
    """As LIRA 2024 R2: the rebuilt model gets the default norms, so the import scrambles
    load case kinds, and a written kind is mapped into the default norms' dictionary."""
    plain_import, plain_write = adapter.import_file, adapter.write_table

    def import_file(path, template=""):
        info = plain_import(path, template)
        rows = [[*r[:3], kinds.get(r[0], r[3]), r[4]] for r in cases]
        adapter.docs[info.path]["tables"]["LoadCases"] = rows
        return info

    def write_table(ref, name, rows, numbers=None):
        if name == "LoadCases":
            rows = [[*r[:3], r[3] if r[3] in SP20 else SNIP_IN_SP20[r[3]], *r[4:]] for r in rows]
        return plain_write(ref, name, rows, numbers)

    adapter.import_file, adapter.write_table = import_file, write_table


def test_other_norms_lost_in_the_rebuild_leave_a_draft_and_a_report(model):
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное(П)", None], [2, "Live", TASK, "Длительное(Д)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = cases
    import_resets_kinds(adapter, {1: "Постоянное ( P)", 2: "Длит. доминир.1 ( Pl1)"}, cases)

    with pytest.raises(LiraError, match="нормы сочетаний") as err:
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "2: Длительное(Д) -> Постоянное ( P)" in str(err.value)
    assert "original model is untouched" in str(err.value)
    assert path.read_bytes() == b"original"
    assert list(adapter.docs) == [str(path)]  # the rebuilt model was closed
    draft = path.with_name("frame_кор_нагр_error.lir")
    report = draft.with_suffix(".txt").read_text(encoding="utf-8")
    assert draft.exists() and str(draft) in str(err.value)
    assert "2: Длительное(Д) -> Постоянное ( P)" in report
    assert "таблица LoadCases отличается: строка 1:" in report
    assert "строка 2: [2, 'Live'" in report and "Исходная модель не изменена" in report


def test_default_norms_kinds_scrambled_by_the_import_are_restored(model):
    adapter, path = model
    cases = [
        [1, "Dead", TASK, "Постоянное ( P)", None],
        [2, "Snow", TASK, "Кратк. доминир.1 ( Pt1)", None],
        [3, "Wind", TASK, "Мгновенное(М)", None],
    ]
    adapter.docs[str(path)]["tables"]["LoadCases"] = cases
    import_resets_kinds(adapter, {2: "Длит. прочие ( Pl)", 3: "Особое ( Ps)"}, cases)

    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert report["tables_restored_after_import"] == ["LoadCases"]
    assert adapter.read_table(str(path), "LoadCases") == cases


def test_loads_change_and_lost_tables_come_back(model):
    adapter, path = model
    restraints = [[1, 1, 1, 1, 1, 1, 1, None]]
    adapter.docs[str(path)]["tables"]["Nodes_Restraints"] = restraints
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = [list(r) for r in cases]
    import_resets_kinds(adapter, {}, cases)  # the import loses Nodes_Restraints

    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert report["loads_now"] == 2
    assert report["tables_restored_after_import"] == ["Nodes_Restraints"]
    assert adapter.read_table(str(path), "Nodes_Restraints") == restraints
    assert adapter.read_table(str(path), "LoadCases") == cases


def test_a_table_that_cannot_be_restored_is_refused(model):
    adapter, path = model
    adapter.docs[str(path)]["tables"]["Nodes_Restraints"] = [[1, 1, None, None]]
    import_resets_kinds(adapter, {})
    adapter.write_table = lambda ref, name, rows, numbers=None: [TableError(0, 1, "bad")]

    with pytest.raises(LiraError, match="did not match") as err:
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "Nodes_Restraints" in str(err.value)
    assert "original model is untouched" in str(err.value)
    assert path.read_bytes() == b"original"
    assert list(adapter.docs) == [str(path)]
    report = path.with_name("frame_кор_нагр_error.txt").read_text(encoding="utf-8")
    assert "таблицу Nodes_Restraints не удалось восстановить: bad" in report


def test_a_table_lira_refuses_to_take_is_refused_plainly(model):
    """As a model with construction stages: LIRA's Apply of LoadCases fails outright."""
    adapter, path = model
    import_resets_kinds(adapter, {1: None, 2: None})
    plain_write = adapter.write_table

    def write_table(ref, name, rows, numbers=None):
        if name == "LoadCases":
            raise LiraError("apply table LoadCases failed: Ошибка.")
        return plain_write(ref, name, rows, numbers)

    adapter.write_table = write_table

    with pytest.raises(LiraError, match="did not match") as err:
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "apply table LoadCases failed" in str(err.value)
    assert "нормы сочетаний" not in str(err.value)
    assert path.read_bytes() == b"original"
    assert list(adapter.docs) == [str(path)]
    assert path.with_name("frame_кор_нагр_error.lir").exists()


def test_a_rebuilt_model_lira_asks_about_is_answered_no_and_kept_as_a_draft(model):
    """As a model with construction stages: writing the rebuilt model as text, LIRA asks
    whether to replace its analysis kind; the server answers No and keeps a draft."""
    adapter, path = model
    import_resets_kinds(adapter, {})
    plain_export = adapter.export_text

    def export_text(ref, declined=None, companions_to=None):
        if ref != str(path):
            declined.append("'Предупреждение': Заменить? (buttons: Да, Нет)")
            return "garbage"
        return plain_export(ref, declined, companions_to)

    adapter.export_text = export_text
    with pytest.raises(LiraError, match="did not match"):
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    report = path.with_name("frame_кор_нагр_error.txt").read_text(encoding="utf-8")
    assert "не удалось выгрузить в текст, чтобы сверить нагрузки: Not a LIRA" in report
    assert "ЛИРА спросила 'Предупреждение': Заменить?" in report
    assert "сервер ответил «Нет»" in report
    assert path.with_name("frame_кор_нагр_error.lir").exists()
    assert path.read_bytes() == b"original"


def test_drafts_are_numbered_and_never_replace_a_file(model):
    adapter, path = model
    import_resets_kinds(adapter, {1: None, 2: None})
    adapter.write_table = lambda ref, name, rows, numbers=None: [TableError(0, 1, "bad")]
    adapter.docs[str(path)]["tables"]["Nodes_Restraints"] = [[1, 1, None, None]]
    path.with_name("frame_кор_нагр_error.lir").write_bytes(b"older draft")
    path.with_name("frame_кор_нагр_error_2.txt").write_text("older report")

    with pytest.raises(LiraError, match=r"frame_кор_нагр_error_3.lir"):
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert path.with_name("frame_кор_нагр_error.lir").read_bytes() == b"older draft"
    assert path.with_name("frame_кор_нагр_error_2.txt").read_text() == "older report"
    assert path.with_name("frame_кор_нагр_error_3.txt").exists()


def test_a_draft_lira_cannot_save_is_reported_and_closed(model):
    adapter, path = model
    import_resets_kinds(adapter, {1: None, 2: None})
    adapter.write_table = lambda ref, name, rows, numbers=None: [TableError(0, 1, "bad")]
    adapter.docs[str(path)]["tables"]["Nodes_Restraints"] = [[1, 1, None, None]]
    plain_save = adapter.save_document

    def save_document(ref, target=None):
        if target and "error" in target:
            raise LiraError("SaveAs failed: disk full")
        return plain_save(ref, target)

    adapter.save_document = save_document
    with pytest.raises(LiraError, match="could not be saved as a draft") as err:
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "disk full" in str(err.value)
    assert list(adapter.docs) == [str(path)]
    assert path.read_bytes() == b"original"


def test_unreadable_load_cases_leave_the_kinds_to_the_full_check(model):
    adapter, path = model
    import_resets_kinds(adapter, {2: "Особое ( Ps)"})
    plain_read = adapter.read_table

    def read_table(ref, name, numbers=None):
        if name == "LoadCases":  # LIRA could not build the table for this model
            raise LiraError("cannot create table LoadCases")
        return plain_read(ref, name, numbers)

    adapter.read_table = read_table

    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert report["loads_now"] == 2


def test_a_failed_save_brings_the_original_model_back(model):
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = [list(r) for r in cases]
    import_resets_kinds(adapter, {}, cases)
    plain_save = adapter.save_document

    def save_document(ref, target=None):
        if target == str(path):
            raise LiraError("SaveAs failed: the file is locked")
        return plain_save(ref, target)

    adapter.save_document = save_document
    with pytest.raises(LiraError) as err:
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "Loads were not changed" in str(err.value) and "file is locked" in str(err.value)
    assert "the original model is open again" in str(err.value)
    assert list(adapter.docs) == [str(path)]  # the rebuilt model was closed
    assert path.read_bytes() == b"original"


def test_the_files_the_export_writes_beside_the_text_go_with_the_import(model, tmp_path):
    """Construction stages (.mnt) and densities and blocks (.ald) come back only when
    the import finds them beside the text, under the text's own name."""
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = [list(r) for r in cases]
    import_resets_kinds(adapter, {}, cases)
    lira = tmp_path / "lira"
    lira.mkdir()
    for name in ("frame.mnt", "frame.ald"):
        (lira / name).write_text("stages", encoding="utf-8")
    adapter.companions["frame"] = [str(lira / "frame.mnt"), str(lira / "frame.ald")]

    set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert adapter.imported_beside == ["frame.ald", "frame.mnt", "frame.txt"]


def test_a_rebuilt_text_that_lost_data_leaves_a_draft(model):
    """What no input table shows, as reinforcement types on elements, is checked in the
    text of the rebuilt model."""
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = [list(r) for r in cases]
    adapter.docs[str(path)]["text"] = FRAME + "( 32/ 1 1/ 2 1/ 3 2/ )\n"
    import_resets_kinds(adapter, {}, cases)
    plain_export = adapter.export_text

    def export_text(ref, declined=None, companions_to=None):
        text = plain_export(ref, declined, companions_to)
        return text if ref == str(path) else text.replace(" 3 2/", "")

    adapter.export_text = export_text
    with pytest.raises(LiraError, match="did not match"):
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    report = path.with_name("frame_кор_нагр_error.txt").read_text(encoding="utf-8")
    assert "раздел 32 (назначение типов армирования (ТЗА)) текста модели отличается" in report
    assert path.read_bytes() == b"original"


def test_numbers_that_differ_only_by_rounding_are_equal(model):
    """A coordinate 4.050000000000001 comes back from the text as 4.05."""
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"] |= {
        "LoadCases": [list(r) for r in cases],
        "Nodes_Coordinates": [[1, 4.050000000000001, 3.67e-16, 0.0]],
    }
    import_resets_kinds(adapter, {}, cases)
    plain_import = adapter.import_file

    def import_file(p, template=""):
        info = plain_import(p, template)
        adapter.docs[info.path]["tables"]["Nodes_Coordinates"] = [[1, 4.05, 0.0, 0.0]]
        return info

    adapter.import_file = import_file
    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])

    assert report["tables_restored_after_import"] == []


def test_entries_the_import_would_set_to_1_are_written_as_0():
    """LIRA's import sets entries 50 and 51 of document 0 to 1 when the text leaves them
    out, and its export leaves them out when they are 0."""
    model = TextModel.parse(FRAME)
    _keep_zero_entries(model)
    assert model.body(0).endswith("C 1/\n50; 0 /\n51; 0 /\n")
    kept = TextModel.parse(FRAME.replace("C 1/", "C 1/ 50; 1 /"))
    _keep_zero_entries(kept)
    assert "50; 1 /" in kept.body(0) and kept.body(0).count("50;") == 1
    assert kept.body(0).endswith("51; 0 /\n")


def test_an_unpacked_scheme_is_packed_only_when_the_user_agreed(model):
    """Deleted nodes keep their numbers until the scheme is packed, and the text would
    bring them back."""
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    tables = adapter.docs[str(path)]["tables"]
    tables["LoadCases"] = [list(r) for r in cases]
    tables["Nodes_Coordinates"] = [[1, 0, 0, 0], [2, 6, 0, 0], [5, 0, 0, 3], [6, 6, 0, 3]]
    import_resets_kinds(adapter, {}, cases)

    with pytest.raises(LiraInputError, match=r"not packed \(4 nodes numbered up to 6\)"):
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])
    assert path.read_bytes() == b"original"

    # Packing renumbers: the loads were given in the old numbering and are not applied.
    text_before = adapter.docs[str(path)]["text"]
    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1], pack=True)
    assert report["packed"] == {"nodes": {"count": 4, "highest_number_before": 6}}
    assert report["loads_changed"] is False and "new numbering" in report["next"]
    assert [r[0] for r in adapter.docs[str(path)]["tables"]["Nodes_Coordinates"]] == [1, 2, 3, 4]
    assert adapter.docs[str(path)]["text"] == text_before

    report = set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1], pack=True)
    assert "packed" not in report and report["load_cases_changed"] == [1]  # applied now


def test_the_rebuilt_model_is_closed_when_the_original_cannot_be(model):
    adapter, path = model
    cases = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    adapter.docs[str(path)]["tables"]["LoadCases"] = [list(r) for r in cases]
    import_resets_kinds(adapter, {}, cases)
    plain_close = adapter.close_document

    def close_document(ref, discard=False):
        if ref == str(path):
            raise LiraError("Close failed: stood in by the test")
        plain_close(ref, discard)

    adapter.close_document = close_document
    with pytest.raises(LiraError, match="Close failed"):
        set_loads(adapter, str(path), [Load(1, 3, 6, 3, [12.5])], [1])
    assert list(adapter.docs) == [str(path)]
    assert path.read_bytes() == b"original"


def test_load_values_past_six_significant_digits_are_equal():
    """LIRA keeps about six significant digits of a load: 10/3 comes back as 3.33333."""
    asked = [Load(1, 3, 6, 3, [10 / 3]), Load(2, 4, 0, 1, [12.345678, 2.0])]
    exported = [Load(2, 4, 0, 1, [12.3457, 2.0, 0.0]), Load(1, 3, 6, 3, [3.33333, 0.0])]
    assert _same_loads(exported, asked)
    assert not _same_loads([Load(1, 3, 6, 3, [3.34]), exported[0]], asked)
    assert not _same_loads(exported[:1], asked)
    # Two loads of one element whose rounded values sort the other way round.
    twins = [Load(1, 3, 6, 3, [3.333333, 1.0]), Load(1, 3, 6, 3, [3.33333, 2.0])]
    rounded = [Load(1, 3, 6, 3, [3.33333, 1.0]), Load(1, 3, 6, 3, [3.33333, 2.0])]
    assert _same_loads(rounded, twins)
    assert not _same_loads(rounded + rounded[:1], twins)  # a load twice is one too many


def test_load_value_rows_lira_merges_are_equal():
    """LIRA's import merges value rows that differ by about 1e-4 relatively (the owner's
    model: 0.000150054 came back as 0.000150069); a change of 0.1 % is no rounding."""
    asked = [Load(8, 4400, 15, 3, [0.0117619, 0.199304, 0.000150054])]
    merged = [Load(8, 4400, 15, 3, [0.0117619, 0.199304, 0.000150069])]
    assert _same_loads(merged, asked)
    assert not _same_loads([Load(8, 4400, 15, 3, [0.0117619, 0.199504, 0.000150054])], asked)


def test_a_million_loads_are_compared_in_seconds():
    """The owner's model has 1.5 million loads: the comparison must not try every pair."""
    import time

    asked = [Load(1 + i % 27, i // 27, 6, 3, [i * 0.001]) for i in range(1_000_000)]
    started = time.monotonic()
    assert _same_loads(list(reversed(asked)), asked)
    assert time.monotonic() - started < 30


def test_the_report_names_loads_that_differ():
    asked = [Load(1, 3, 6, 3, [12.5]), Load(1, 7, 0, 3, [2.0]), Load(2, 4, 6, 3, [1.0])]
    got = [Load(1, 3, 6, 3, [12.5]), Load(1, 7, 0, 3, [2.5])]
    report = _loads_problem(got, asked)
    assert "нет 2 запрошенных" in report
    assert "загружение 1, узел 7, тип 0, направление 3, значения 2;" in report
    assert "загружение 2, элемент 4" in report
    assert "есть 1 лишних (например: загружение 1, узел 7, тип 0, " in report
    assert report.endswith("значения 2.5)")
    assert _loads_problem(asked, list(reversed(asked))) is None


def test_tables_are_equal_up_to_single_precision():
    """LIRA keeps bed coefficients C1/C2 in single precision: 145.84943 reads 145.84944."""
    assert _same([[16462, 2220.3062, 145.84943]], [[16462, 2220.3062, 145.84944]])
    assert not _same([[16462, 2220.3062, 145.84943]], [[16462, 2220.3062, 145.8496]])
    assert not _same([[1_000_000, 0.0]], [[1_000_001, 0.0]])  # numbers stay exact


def test_only_the_loads_on_the_given_elements_and_nodes_change(model):
    adapter, path = model
    adapter.docs[str(path)]["tables"]["LoadCases"] = [[1, "Dead", TASK, "Постоянное ( P)", None]]
    only = Targets(elements=frozenset({2, 3}))

    assert read_loads(adapter, str(path), None, Targets(nodes=frozenset({4}))) == [
        {"case": 2, "target": 4, "type": 0, "direction": 1, "values": [5.0], "on_node": True}
    ]
    report = set_loads(adapter, str(path), [Load(1, 2, 6, 3, [7.0])], [1], targets=only)

    now = {(ld["case"], ld["target"]): ld["values"] for ld in read_loads(adapter, str(path))}
    assert report["loads_now"] == 2
    assert now == {(1, 2): [7.0], (2, 4): [5.0]}  # bar 3 lost its load, node 4 kept its


def test_loads_outside_the_given_targets_are_refused(model):
    adapter, path = model
    with pytest.raises(LiraInputError, match=r"Loads on \[1\] are outside"):
        set_loads(
            adapter, str(path), [Load(1, 1, 6, 3, [7.0])], [1], targets=Targets(frozenset({2}))
        )
    assert path.read_bytes() == b"original"


def test_a_load_type_of_unknown_target_kind_stops_a_partial_change(model):
    adapter, path = model
    doc = adapter.docs[str(path)]
    doc["text"] = FRAME.replace("( 6/ 3 6 3 1 1/", "( 6/ 3 6 3 1 1/ 2 40 3 1 1/")
    only = Targets(elements=frozenset({2}))

    read = read_loads(adapter, str(path), None, only)
    with pytest.raises(LiraInputError, match=r"Load types \[40\]"):
        set_loads(adapter, str(path), [Load(1, 2, 6, 3, [1.0])], [1], targets=only)

    assert [(ld["target"], ld["type"], ld.get("target_kind_unknown")) for ld in read] == [
        (2, 40, True)
    ]
    assert path.read_bytes() == b"original"


def test_type_60_loads_a_node_not_the_element_of_that_number(model):
    adapter, path = model
    adapter.docs[str(path)]["text"] = FRAME.replace(
        "( 6/ 3 6 3 1 1/", "( 6/ 3 6 3 1 1/ 2 60 3 1 1/"
    )

    on_bar = read_loads(adapter, str(path), None, Targets(elements=frozenset({2})))
    on_node = read_loads(adapter, str(path), None, Targets(nodes=frozenset({2})))

    assert on_bar == []
    assert [(ld["target"], ld["type"], ld["on_node"]) for ld in on_node] == [(2, 60, True)]
