import shutil
import uuid
from pathlib import Path

import pytest

from lira_sapr_mcp.errors import LiraError
from lira_sapr_mcp.services import loads as loads_service
from lira_sapr_mcp.services.textformat import Load, TextModel

pytestmark = pytest.mark.lira

EXAMPLE = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
EXAMPLE = EXAMPLE / "01_плоская рама ЖБ" / "01_2D рама ЖБ.lir"

# Portal frame 6 x 3 m, fixed bases, 40x60 cm bars, 10 t/m uniform load on the beam.
FRAME = """( 0/ 1; FRAME/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )
( 1/ 10 1 1 3/ 10 1 2 4/ 10 1 3 4/ )
( 3/ 1 S0 3E6 40 60/ 0 RO 2.5/ )
( 4/ 0 0 0/ 6 0 0/ 0 0 3/ 6 0 3/ )
( 5/ 1 1 2 3 4 5 6/ 2 1 2 3 4 5 6/ )
( 6/ 3 6 3 1 1/ )
( 7/ 1 10/ )
"""


@pytest.fixture
def example(workdir):
    if not EXAMPLE.exists():
        pytest.skip("LIRA example models are not installed")
    model = workdir / "lmcp_example.lir"
    shutil.copy(EXAMPLE, model)
    return model


def test_text_export_contains_loads_and_spares_user_files(lira, adapter, example):
    info, _ = lira(adapter.open_document, str(example))
    data_dir = Path(adapter.directories()["TextDataDirectory"])
    users_file = data_dir / f"{info.title}.txt"
    users_file.write_text("owner's own file", encoding="utf-8")
    try:
        text = lira(adapter.export_text, str(example))
        assert users_file.read_text(encoding="utf-8") == "owner's own file"
    finally:
        users_file.unlink()
    loads = TextModel.parse(text).loads()
    assert {ld.case for ld in loads} == {1, 2, 3, 4}


def test_new_model_from_text_keeps_loads(lira, adapter, workdir):
    target = workdir / "lmcp_frame.lir"
    info = lira(loads_service.import_text_model, adapter, FRAME, str(target))
    assert info.saved and Path(info.path) == target
    loads = lira(loads_service.read_loads, adapter, str(target))
    # LIRA pads the value row with zeros.
    assert loads == [
        {
            "case": 1,
            "target": 3,
            "type": 6,
            "direction": 3,
            "values": [10.0, 0.0, 0.0, 0.0],
            "on_node": False,
        }
    ]
    assert not lira(adapter.document_info, str(target)).modified


def as_loads(dicts):
    return [Load(**{k: v for k, v in d.items() if k != "on_node"}) for d in dicts]


def test_changing_loads_of_own_model(lira, adapter, workdir):
    """A model with LIRA's default norms survives the rebuild: loads change, rest stays."""
    model = workdir / "lmcp_frame2.lir"
    two_cases = FRAME.replace("( 6/ 3 6 3 1 1/ )", "( 6/ 3 6 3 1 1/ 4 0 1 2 2/ )").replace(
        "( 7/ 1 10/ )", "( 7/ 1 10/ 2 5/ )"
    )
    lira(loads_service.import_text_model, adapter, two_cases, str(model))
    before = {n: lira(adapter.read_table, str(model), n) for n in ("LoadCases", "Stiffnesses")}

    report = lira(loads_service.set_loads, adapter, str(model), [Load(1, 3, 6, 3, [12.5])], [1])

    assert report["loads_now"] == 2
    after = sorted(
        (ld.case, ld.target, ld.values[0])
        for ld in as_loads(lira(loads_service.read_loads, adapter, str(model)))
    )
    assert after == [(1, 3, 12.5), (2, 4, 5.0)]
    assert not lira(adapter.document_info, str(model)).modified
    for name, rows in before.items():
        assert lira(adapter.read_table, str(model), name) == rows


def test_loads_with_many_digits_are_changed(lira, adapter, workdir):
    """LIRA keeps about six significant digits of a load: 10/3 comes back as 3.33333."""
    model = workdir / "lmcp_frame_digits.lir"
    lira(loads_service.import_text_model, adapter, FRAME, str(model))

    lira(loads_service.set_loads, adapter, str(model), [Load(1, 3, 6, 3, [10 / 3])], [1])

    loads = as_loads(lira(loads_service.read_loads, adapter, str(model)))
    assert [ld.values[0] for ld in loads] == [pytest.approx(10 / 3, rel=1e-5)]


def test_a_failed_save_reopens_the_original_model(lira, adapter, workdir, monkeypatch):
    """LIRA closed the original before saving the rebuilt model over it; if that save
    fails (stood in for here), the rebuilt model goes and the original is open again."""
    model = workdir / "lmcp_frame_save.lir"
    lira(loads_service.import_text_model, adapter, FRAME, str(model))
    original = model.read_bytes()
    plain_save = adapter.save_document

    def save_document(ref, path=None):
        if path == str(model):
            raise LiraError("SaveAs failed: stood in by the test")
        return plain_save(ref, path)

    monkeypatch.setattr(adapter, "save_document", save_document)
    with pytest.raises(LiraError, match="the original model is open again"):
        lira(loads_service.set_loads, adapter, str(model), [Load(1, 3, 6, 3, [12.5])], [1])
    open_now = [d for d in lira(adapter.list_documents) if d.title == model.stem]
    assert [d.path.lower() for d in open_now] == [str(model).lower()]
    assert model.read_bytes() == original
    loads = as_loads(lira(loads_service.read_loads, adapter, str(model)))
    assert [(ld.case, ld.target, ld.values[0]) for ld in loads] == [(1, 3, 10.0)]


def test_changing_loads_keeps_kinds_of_default_norms(lira, adapter, workdir):
    """A model under LIRA's default norms (SP 20.13330) whose engineer set various load
    case kinds: the text import scrambles them, the restore of LoadCases brings them back."""
    model = workdir / "lmcp_frame_kinds.lir"
    four_cases = FRAME.replace(
        "( 6/ 3 6 3 1 1/ )", "( 6/ 3 6 3 1 1/ 4 0 1 2 2/ 4 0 1 3 3/ 4 0 1 4 4/ )"
    ).replace("( 7/ 1 10/ )", "( 7/ 1 10/ 2 5/ 3 2/ 4 1/ )")
    lira(loads_service.import_text_model, adapter, four_cases, str(model))
    kinds = {
        1: "Постоянное ( P)",
        2: "Кратк. доминир.1 ( Pt1)",
        3: "Особое ( Ps)",
        4: "Мгновенное(М)",
    }
    rows = [
        [*r[:3], kinds[r[0]], *r[4:]] for r in lira(adapter.read_table, str(model), "LoadCases")
    ]
    assert lira(adapter.write_table, str(model), "LoadCases", rows) == []
    lira(adapter.save_document, str(model))
    cases = lira(adapter.read_table, str(model), "LoadCases")
    assert [r[3] for r in cases] == list(kinds.values())

    report = lira(loads_service.set_loads, adapter, str(model), [Load(1, 3, 6, 3, [12.5])], [1])

    assert "LoadCases" in report["tables_restored_after_import"]  # the import scrambled them
    assert lira(adapter.read_table, str(model), "LoadCases") == cases
    loads = lira(loads_service.read_loads, adapter, str(model), [1])
    assert [(d["target"], d["values"][0]) for d in loads] == [(3, 12.5)]


def test_mismatching_rebuild_leaves_the_original_and_a_draft(lira, adapter, example):
    """The example uses other combination norms (SNiP 2.01.07-85*) than LIRA's default, so
    its load case kinds cannot be restored after the rebuild: the original must stay as it
    was, and the rebuilt model with the new loads is kept as a draft with a report."""
    lira(adapter.open_document, str(example))
    original_bytes = example.read_bytes()
    original = as_loads(lira(loads_service.read_loads, adapter, str(example)))
    changed = [
        Load(ld.case, ld.target, ld.type, ld.direction, [v * 2 for v in ld.values])
        for ld in original
        if ld.case == 1
    ]

    with pytest.raises(LiraError, match="нормы сочетаний") as err:
        lira(loads_service.set_loads, adapter, str(example), changed, [1])

    assert "2: Длительное(Д) -> Постоянное ( P)" in str(err.value)
    assert "original model is untouched" in str(err.value)

    assert example.read_bytes() == original_bytes
    assert as_loads(lira(loads_service.read_loads, adapter, str(example))) == original
    assert [d.path.lower() for d in lira(adapter.list_documents)].count(str(example).lower()) == 1
    draft = example.with_name("lmcp_example_кор_нагр_error.lir")
    report = draft.with_suffix(".txt").read_text(encoding="utf-8")
    assert "Длительное(Д) -> Постоянное ( P)" in report
    lira(adapter.open_document, str(draft))
    in_draft = as_loads(lira(loads_service.read_loads, adapter, str(draft), [1]))
    lira(adapter.close_document, str(draft), True)
    assert loads_service._same_loads(in_draft, changed)


def test_a_model_with_construction_stages_keeps_stages_and_history(lira, adapter, workdir):
    """Example 16: the stages come back from the .mnt file beside the text, so LIRA adds
    no load cases to the construction history, whose order stays. The rebuild still
    loses the safety factors the example sets by hand in DCL, reinforcement of a
    nonlinear stiffness and the reinforcement types of elements 11-26 (the last two only
    the text shows): a draft with a report."""
    source = next(EXAMPLE.parents[1].glob("16_*/ПО_ЖБ_р_210ш_dT002_1o_1э.lir"), None)
    if source is None:
        pytest.skip("LIRA example 16 is not installed")
    # A fresh name: LIRA's own leftovers of a name (.mnt, .ald) mark a model as changed.
    model = workdir / f"lmcp_stages_{uuid.uuid4().hex[:6]}.lir"
    shutil.copy(source, model)
    lira(adapter.open_document, str(model))
    # The example is not packed (22 elements numbered up to 26): the user agrees to pack
    # it, and the loads are read again in the new numbering.
    with pytest.raises(LiraError, match=r"not packed \(22 elements numbered up to 26\)"):
        lira(loads_service.set_loads, adapter, str(model), [], [])
    packed = lira(loads_service.set_loads, adapter, str(model), [], [], True)
    assert packed["loads_changed"] is False
    assert lira(adapter.numbering_gaps, str(model)) == {}
    original_bytes = model.read_bytes()
    tables = {n: lira(adapter.read_table, str(model), n) for n in ("LoadCases", "AssemblageStages")}
    history = TextModel.parse(lira(adapter.export_text, str(model))).body(16)
    loads = as_loads(lira(loads_service.read_loads, adapter, str(model)))
    doubled = [Load(ld.case, ld.target, ld.type, ld.direction, [v * 2 for v in ld.values])
               for ld in loads if ld.case == 1]  # fmt: skip

    with pytest.raises(LiraError, match="did not match") as err:
        lira(loads_service.set_loads, adapter, str(model), doubled, [1])

    assert model.read_bytes() == original_bytes
    draft = model.with_name(f"{model.stem}_кор_нагр_error.lir")
    report = draft.with_suffix(".txt").read_text(encoding="utf-8")
    assert draft.exists() and str(draft) in str(err.value)
    tables_named = [line for line in report.splitlines() if "таблиц" in line]
    assert len(tables_named) == 1 and tables_named[0].startswith("- таблица DCL отличается")
    assert "раздел 16" not in report  # the construction history keeps its load cases
    assert "раздел 3 (жёсткости)" in report and "раздел 32 (" in report
    lira(adapter.open_document, str(draft))
    try:
        for name, rows in tables.items():
            assert loads_service._same(lira(adapter.read_table, str(draft), name), rows), name
        text = TextModel.parse(lira(adapter.export_text, str(draft), declined=[]))
        assert text.body(16) == history
        wanted = doubled + [ld for ld in loads if ld.case != 1]
        assert loads_service._same_loads(text.loads(), wanted)
    finally:
        lira(adapter.close_document, str(draft), True)


@pytest.mark.parametrize("name", ["lmcp_linear_stages_{}", "lmcp_linear_stages(v{})"])
def test_a_linear_model_with_construction_stages_is_rebuilt(lira, adapter, workdir, name):
    """The portal frame with linear bars in two stages (columns, then the beam): the
    rebuild asks nothing, keeps the stages and the construction history, and replaces
    the original. LIRA names the stages file of a model with ( ) in its name with { }."""
    model = workdir / f"{name.format(uuid.uuid4().hex[:6])}.lir"
    two_cases = FRAME.replace("( 6/ 3 6 3 1 1/ )", "( 6/ 3 6 3 1 1/ 4 0 1 2 2/ )").replace(
        "( 7/ 1 10/ )", "( 7/ 1 10/ 2 5/ )"
    )
    lira(loads_service.import_text_model, adapter, two_cases, str(model))
    stages = [[1, "1 2", None], [2, "3", None]]
    assert lira(adapter.write_table, str(model), "AssemblageStages", stages) == []
    lira(adapter.save_document, str(model))
    history = TextModel.parse(lira(adapter.export_text, str(model))).body(16)
    assert history is not None  # the stages made LIRA's construction history

    report = lira(loads_service.set_loads, adapter, str(model), [Load(1, 3, 6, 3, [20.0])], [1])

    assert report["loads_now"] == 2
    text = TextModel.parse(lira(adapter.export_text, str(model)))  # asks nothing
    assert text.body(16) == history
    assert sorted((ld.case, ld.target, ld.values[0]) for ld in text.loads()) == [
        (1, 3, 20.0),
        (2, 4, 5.0),
    ]
    got = lira(adapter.read_table, str(model), "AssemblageStages")
    assert [[n, " ".join(e.split()), r] for n, e, r in got] == stages
