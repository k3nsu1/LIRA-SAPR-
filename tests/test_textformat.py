import pytest

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.services.textformat import Load, TextModel

# Shaped like LIRA's own export (command "Create text file"), including names with
# parentheses and a byte-order mark.
SAMPLE = """﻿( 0/ 1; frame/ 2; 5/
33;M 1 CM 100 T 1 C 1 /
39;
1: DEAD (P) ;
2: LIVE ;
 /
)
( 1/
10 1 1 3 /10 1 2 4 /10 1 3 4 /
 )
( 4/
0 0 0 /6 0 0 /0 0 3 /6 0 3 /
 )
( 6/
3 6 3 1 1 /4 0 1 2 2 /
 3 16 3 3 2 /
 )
( 7/
1 10 /
2 -1.5 0 /
3 2 0 4.5 3 /
 )
( 8/
 0 0 0 0 0 0 0 1.10 1.00 1.00 1.00 0.90 1.00 /
 )
"""


def test_documents_are_split_by_number():
    m = TextModel.parse(SAMPLE)
    assert [n for n, _ in m.docs] == [0, 1, 4, 6, 7, 8]
    assert "DEAD (P)" in m.body(0)


def test_render_keeps_untouched_documents():
    m = TextModel.parse(SAMPLE)
    again = TextModel.parse(m.render())
    assert again.docs == m.docs


def test_loads_join_documents_6_and_7():
    loads = TextModel.parse(SAMPLE).loads()
    assert loads == [
        Load(1, 3, 6, 3, [10.0]),
        Load(2, 4, 0, 1, [-1.5, 0.0]),
        Load(2, 3, 16, 3, [2.0, 0.0, 4.5, 3.0]),
    ]
    assert loads[1].on_node


def test_set_loads_rewrites_only_6_and_7():
    m = TextModel.parse(SAMPLE)
    loads = m.loads()
    loads[0].values = [12.5]
    loads.append(Load(1, 2, 6, 3, [12.5]))
    m.set_loads(loads)
    again = TextModel.parse(m.render())
    assert sorted(again.loads(), key=lambda ld: (ld.case, ld.target)) == sorted(
        loads, key=lambda ld: (ld.case, ld.target)
    )
    assert again.body(7).count("12.5") == 1  # equal value rows are shared
    assert again.body(8) == m.body(8) and again.body(1) == m.body(1)


def test_removing_all_loads_drops_documents():
    m = TextModel.parse(SAMPLE)
    m.set_loads([])
    assert m.body(6) is None and m.body(7) is None


def test_unknown_syntax_fails_loudly():
    bad = SAMPLE.replace("3 6 3 1 1 /", "1-3 6 3 1 1 /")
    with pytest.raises(LiraInputError, match="document 6"):
        TextModel.parse(bad).loads()


def test_not_a_text_model():
    with pytest.raises(LiraInputError):
        TextModel.parse("hello")
