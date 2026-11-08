import pytest

pytestmark = pytest.mark.lira

NODES = [[1, 0, 0, 0], [2, 6, 0, 0], [3, 0, 0, 3], [4, 6, 0, 3]]
ELEMENTS = [[1, 10, "1, 3"], [2, 10, "2, 4"], [3, 10, "3, 4"]]


def frame(lira, adapter):
    doc = lira(adapter.create_document, "lmcp_new_frame", 5, "test frame")
    assert lira(adapter.write_table, doc.path, "Nodes_Coordinates", NODES) == []
    assert lira(adapter.write_table, doc.path, "Elements_TypeAndNodes", ELEMENTS) == []
    return doc


def table_count(lira, adapter, ref):
    return lira(lambda: adapter.find_document(ref).AllTables.ItemCount)


def test_capabilities_come_from_the_type_library(adapter):
    assert {"OpenDocument", "ImportFile", "StartAnalysis", "StartCmd"} <= adapter.capabilities
    assert {"SetDeadWeightLoad", "Select"} <= adapter.capabilities  # document interface


def test_lira_2024_r2_can_run_every_tool(adapter, tmp_path):
    from fakes import fake_session
    from lira_sapr_mcp.server import build_server
    from lira_sapr_mcp.tools.common import unavailable_tools

    build_server(fake_session(tmp_path)[0])  # registers the tools and what they need
    assert unavailable_tools(adapter.capabilities) == []
    assert {"Nodes_Coordinates", "ElevationMarks", "LoadCases"} <= set(adapter.table_kinds)


def test_create_save_reopen_round_trip(lira, adapter, workdir):
    doc = frame(lira, adapter)
    path = str(workdir / "lmcp_frame_round.lir")
    saved = lira(adapter.save_document, doc.path, path)
    assert saved.saved and saved.path.lower() == path.lower()
    lira(adapter.close_document, path)
    info, _ = lira(adapter.open_document, path)
    assert info.title == "lmcp_frame_round"
    assert lira(adapter.read_table, path, "Nodes_Coordinates") == NODES
    listed = {d.path.lower(): d for d in lira(adapter.list_documents)}
    assert listed[path.lower()].active


def test_scratch_tables_are_removed(lira, adapter):
    doc = frame(lira, adapter)
    lira(adapter.read_table, doc.path, "Elements_TypeAndNodes")
    assert table_count(lira, adapter, doc.path) == 0


def test_scratch_tables_left_behind_are_removed_by_the_next_read(lira, adapter):
    doc = frame(lira, adapter)
    kind = adapter.table_kind("Nodes_Coordinates")

    def leave(name):
        tables = adapter.find_document(doc.path).AllTables
        tables.CreateNewItem(kind, None, adapter._c.kLiraModelPart_All, name, -1)

    def names():
        tables = adapter.find_document(doc.path).AllTables
        return [str(tables.Item(i).Name) for i in range(tables.ItemCount)]

    # An older server's table, one of an ended server process, and the owner's own.
    for name in ("lira-mcp-1a2b3c4d", "lira-mcp-4000000000-eeee5555", "Owner's nodes"):
        lira(leave, name)
    assert len(lira(names)) == 3
    lira(adapter.read_table, doc.path, "Nodes_Coordinates")
    assert lira(names) == ["Owner's nodes"]


def test_bad_rows_are_reported_and_not_applied(lira, adapter):
    doc = frame(lira, adapter)
    errors = lira(adapter.write_table, doc.path, "Elements_TypeAndNodes", [[1, 10, "1, 99"]])
    assert errors
    assert lira(adapter.read_table, doc.path, "Elements_TypeAndNodes") == ELEMENTS


def test_rows_for_selected_numbers_only(lira, adapter):
    doc = frame(lira, adapter)
    assert lira(adapter.read_table, doc.path, "Nodes_Coordinates", [2, 4]) == [NODES[1], NODES[3]]
    assert lira(adapter.write_table, doc.path, "Nodes_Coordinates", [[4, 6, 0, 4]], [4]) == []
    rows = lira(adapter.read_table, doc.path, "Nodes_Coordinates")
    assert rows == [*NODES[:3], [4, 6, 0, 4]]


def test_appending_a_row_adds_a_node(lira, adapter):
    doc = frame(lira, adapter)
    lira(adapter.write_table, doc.path, "Nodes_Coordinates", [*NODES, [5, 3, 0, 3]])
    assert len(lira(adapter.read_table, doc.path, "Nodes_Coordinates")) == 5


def test_units_are_named(lira, adapter):
    units = lira(adapter.units)
    assert units.geometry in {"m", "cm", "mm", "ft", "in"}
