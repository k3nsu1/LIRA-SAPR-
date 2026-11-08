"""Comparing the whole text of a rebuilt model with the text it was built from."""

from lira_sapr_mcp.services.textcheck import text_differences

# As LIRA writes a model with construction stages and physically nonlinear stiffnesses
# (example 16, cut down).
ORIGINAL = """( 0/ 1; frame/ 2; 2/
39;
1: ЭТАЖ 1 ;
2: ЭТАЖ 2 ;
 /
40;"frame.mnt"/
52; 0.001 /
)
( 3/
1 S100 60 40/
 0 NSG 2/
 0 NSAG 1 1  1 3 /
)
( 6/ 1 0 3 1 1/ )
( 7/ 1 10/ )
( 16/
1 1 5 0 300 1  0.2 0.2 0.2 0.2 0.2 0  0 1 /
2 1 5 0 300 1  0.0538 0.0538 0.0538 0.0538 0.0538 0  0 1 /
 )
( 18/
 1 3 4.9087 -27 3 /
 2 14 -0.0035 -2494.9 -0.0015 -2494.9 0 0 /
 3 3 1.5394 -2.3333 3 /
 )
( 32/
  1 1/  2 1/  11 2/
 )
( 153/
 )
"""
# The same model rebuilt by LIRA: other name, nonlinear laws renumbered (and the
# stiffness's references with them), the empty document 153 gone.
REBUILT = """( 0/ 1; lira-mcp-x/ 2; 2/
39;
1: ЭТАЖ 1 ;
2: ЭТАЖ 2 ;
 /
40;"lira-mcp-x.mnt"/
52; 0.001 /
)
( 3/
1 S100 60 40/
 0 NSG 1/
 0 NSAG 1 1  3 2 /
)
( 6/ 1 0 3 1 2/ )
( 7/ 1 20/ )
( 16/
1 1 5 0 300 1  0.2 0.2 0.2 0.2 0.2 0  0 1 /
2 1 5 0 300 1  0.0538 0.0538 0.0538 0.0538 0.0538 0  0 1 /
 )
( 18/
 1 14 -0.0035 -2494.9 -0.0015 -2494.9 0 0 /
 2 3 1.5394 -2.3333 3 /
 3 3 4.9087 -27 3 /
 )
( 32/
  1 1/  2 1/  11 2/
 )
"""


def test_what_lira_changes_on_every_rebuild_is_no_difference():
    assert text_differences(ORIGINAL, REBUILT) == []


def test_lost_reinforcement_of_a_nonlinear_stiffness_is_a_difference():
    lost = REBUILT.replace(" 0 NSAG 1 1  3 2 /\n", "")
    [line] = text_differences(ORIGINAL, lost)
    assert line.startswith("раздел 3 (жёсткости) текста модели отличается: запись 3")
    assert "NSAG 1 1 [3 4.9087 -27 3] [3 1.5394 -2.3333 3]" in line
    assert "записей было 3, стало 2" in line


def test_a_reinforcement_pointing_at_another_law_is_a_difference():
    other = REBUILT.replace("NSAG 1 1  3 2", "NSAG 1 1  3 3")
    assert [d[:9] for d in text_differences(ORIGINAL, other)] == ["раздел 3 "]


def test_a_changed_order_of_the_construction_history_is_a_difference():
    history = REBUILT.split("( 16/")[1].split(")")[0]
    first, second = (line for line in history.splitlines() if line.strip().endswith("/"))
    swapped = REBUILT.replace(
        history, history.replace(first, "#").replace(second, first).replace("#", second)
    )
    [line] = text_differences(ORIGINAL, swapped)
    assert line.startswith("раздел 16 (нелинейные и монтажные загружения) текста модели")


def test_lost_reinforcement_types_of_elements_are_a_difference():
    lost = REBUILT.replace("  11 2/", "")
    assert text_differences(ORIGINAL, lost) == [
        "раздел 32 (назначение типов армирования (ТЗА)) текста модели отличается: запись 3 "
        "была «11 2», стала «нет»; записей было 3, стало 2"
    ]


def test_a_lost_or_new_document_is_named():
    assert text_differences(ORIGINAL, REBUILT.replace("( 32/\n  1 1/  2 1/  11 2/\n )\n", "")) == [
        "раздел 32 (назначение типов армирования (ТЗА)) текста модели пропал"
    ]
    assert text_differences(ORIGINAL, REBUILT + "( 8/ 0 0 0 0 0 0 0 1.1 1 /\n )\n") == [
        "раздел 8 (таблица РСУ) текста модели появился: «0 0 0 0 0 0 0 1.1 1»"
    ]


def test_entries_the_import_adds_are_a_difference():
    """LIRA's import sets entries 50 and 51 to 1 when the text leaves them out."""
    added = REBUILT.replace("52; 0.001 /", "50; 1 /\n51; 1 /\n52; 0.001 /")
    [line] = text_differences(ORIGINAL, added)
    assert line.startswith("раздел 0 (общие данные) текста модели отличается: запись 5")


def test_loads_are_left_to_their_own_check():
    assert text_differences(ORIGINAL, REBUILT.replace("1 20/", "1 30/")) == []


def test_numbers_past_six_significant_digits_are_equal():
    """LIRA writes a density of 2.5 of the rebuilt model as 2.50001."""
    noisy = REBUILT.replace("1 S100 60 40/", "1 S100 60 40/\n 0 RO 2.50001/")
    original = ORIGINAL.replace("1 S100 60 40/", "1 S100 60 40/\n 0 RO 2.5/")
    assert text_differences(original, noisy) == []
    assert text_differences(original, noisy.replace("2.50001", "2.5025")) != []


def test_node_and_element_numbers_are_exact():
    """A relative tolerance would take node 100001 for node 100000."""
    original = ORIGINAL.replace("1 S100 60 40/", "1 S100 60 40/\n 0 RO 100000/")
    assert text_differences(original, original.replace("RO 100000", "RO 100001")) != []
