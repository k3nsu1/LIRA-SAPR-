"""Regression checks for information lost when fitting combinations."""

import json
from pathlib import Path

import pytest

from fakes import FakeAdapter
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services import check, rpz
from test_model_check import checked_model


def example(dynamic=False):
    adapter = FakeAdapter()
    checked_model(adapter)
    adapter.docs["f.lir"]["tables"]["LoadCases"] = [
        [1, "first", None, None],
        [2, "second", None, None],
    ]
    adapter.node_displacements = lambda *args: [
        {"node": 1, "case": 1, "X": 1.0},
        {"node": 1, "case": 2, "Y": 1e-8} | ({"shape": 1} if dynamic else {}),
    ]
    adapter.combination_displacements = lambda *args: [
        {"node": 1, "combination": 5, "X": 1.0, "Y": 0.0 if dynamic else 1e-8}
    ]
    return adapter


def test_small_displacement_with_full_coefficient_is_not_exact():
    row = rpz.model_facts(example(), "f.lir")["combinations"][0]
    assert not row["exact"]
    assert row["discarded_cases"] == [2]


def test_small_displacement_may_carry_a_large_reaction():
    with pytest.raises(LiraInputError, match=r"discarded.*2"):
        rpz.reactions(example(), "f.lir", [1], None, 5)


@pytest.mark.parametrize("dynamic", [False, True])
def test_membership_of_unresolved_cases_is_unknown(dynamic):
    findings = check.check_model(example(dynamic), "f.lir")["findings"]
    row = next(f for f in findings if f["check"] == "every load case is in some RSN")
    assert row["ok"] is None


def test_json_preserves_excluded_dynamic_cases(tmp_path):
    facts = rpz.model_facts(example(dynamic=True), "f.lir")
    path = Path(rpz.write_facts_json(facts, tmp_path))
    assert json.loads(path.read_text(encoding="utf-8"))["dynamic_cases_left_out"] == [2]
