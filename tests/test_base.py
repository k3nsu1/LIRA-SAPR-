from lira_sapr_mcp.adapters.base import TableError, normalize_rows, parse_table_errors
from lira_sapr_mcp.adapters.com_adapter import com_message


def test_apply_errors_are_parsed_per_cell():
    text = "0\t2\tNode 99 does not exist\r\n3\t1\tBad type\r\n"
    assert parse_table_errors(text) == [
        TableError(0, 2, "Node 99 does not exist"),
        TableError(3, 1, "Bad type"),
    ]


def test_unstructured_error_text_is_kept():
    assert parse_table_errors("something broke") == [TableError(-1, -1, "something broke")]


def test_empty_cells_become_none():
    assert normalize_rows(((1, "1", "", None),)) == [[1, "1", None, None]]
    assert normalize_rows(None) == []


def test_com_message_prefers_the_description():
    class FakeComError(Exception):
        pass

    exc = FakeComError(-1, "Ошибка.", (0, None, "Error catched in file: Api.cpp\n", None, 0, -1))
    assert com_message(exc) == "Error catched in file: Api.cpp"
    assert com_message(FakeComError(-1, "Ошибка.", None)) == "Ошибка."
