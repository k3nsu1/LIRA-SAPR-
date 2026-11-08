"""Finding elements by criteria: FE type, stiffness, level, structural block, material."""

import pytest
from mcp import Client

from fakes import FakeAdapter, fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.elements import Criteria, find_elements

pytestmark = pytest.mark.anyio

# Two storeys of 3 m: columns 1-4 (nodes 1-8), slab plates 5-6 at Z 3 (nodes 5-10),
# a beam 7 at Z 3 and a plate 8 that is not level (Z 3 to 6).
NODES = [
    [1, 0, 0, 0], [2, 6, 0, 0], [3, 0, 6, 0], [4, 6, 6, 0],
    [5, 0, 0, 3], [6, 6, 0, 3], [7, 0, 6, 3], [8, 6, 6, 3],
    [9, 3, 0, 3], [10, 3, 6, 3], [11, 0, 0, 6], [12, 6, 0, 6],
]  # fmt: skip
ELEMENTS = [
    [1, 10, "1, 5"], [2, 10, "2, 6"], [3, 10, "3, 7"], [4, 10, "4, 8"],
    [5, 44, "5, 9, 10, 7"], [6, 44, "9, 6, 8, 10"], [7, 10, "5, 6"],
    [8, 44, "5, 6, 12, 11"],
]  # fmt: skip
STIFFNESS = [[1, 1], [2, 1], [3, 1], [4, 2], [5, 3], [6, 3], [7, 4], [8, 3]]
BLOCKS = [["1-4", "КОЛОННА", 1, 1, "К1", None, None], ["5 6", "ПЛИТА", 2, 2, "П1", None, None]]
MATERIALS = [[5, "RC", "1.2.1"], [6, "RC", "1.3.1"], [1, "M", "1.1.1"]]


def model(adapter: FakeAdapter) -> str:
    adapter.docs["m.lir"] = {
        "title": "m",
        "path": "m.lir",
        "type": 5,
        "modified": False,
        "tables": {
            "Nodes_Coordinates": NODES,
            "Elements_TypeAndNodes": ELEMENTS,
            "Elements_Stiffnesses": STIFFNESS,
            "StructuralBlocks": BLOCKS,
            "Elements_Materials": MATERIALS,
        },
    }
    adapter.active = "m.lir"
    return "m.lir"


def found(**criteria) -> list[int]:
    adapter = FakeAdapter()
    return find_elements(adapter, model(adapter), Criteria(**criteria))["elements"]


def test_each_criterion():
    assert found(fe_types=(44,)) == [5, 6, 8]
    assert found(stiffnesses=(1, 2)) == [1, 2, 3, 4]
    assert found(z=(3.0, 3.0)) == [5, 6, 7]  # plate 8 rises to Z 6
    assert found(z=(3.0005, 3.0005)) == [5, 6, 7]  # within the tolerance
    assert found(z=(3.01, 3.01)) == []
    assert found(z=(0.0, 3.0), fe_types=(10,)) == [1, 2, 3, 4, 7]
    assert found(block_kind="плита") == [5, 6]
    assert found(mark="к1", floor="1") == [1, 2, 3, 4]
    assert found(material="RC") == [5, 6]
    assert found(material="RC 1.3.1") == [6]
    assert found(within=(5, 7, 8)) == [5, 7, 8]


def test_criteria_combine_and_report_what_was_found():
    adapter = FakeAdapter()
    res = find_elements(adapter, model(adapter), Criteria(fe_types=(44,), z=(3.0, 3.0)))
    assert res == {
        "elements": [5, 6],
        "by_fe_type": {44: 2},
        "by_stiffness": {3: 2},
        "z_range": [3.0, 3.0],
    }


def test_no_criterion_is_refused():
    adapter = FakeAdapter()
    with pytest.raises(LiraInputError, match="at least one criterion"):
        find_elements(adapter, model(adapter), Criteria())


async def test_tool_gives_ranges_and_searches_within_a_fragment(tmp_path):
    session, adapter = fake_session(tmp_path)
    model(adapter)
    adapter.docs["m.lir"]["visible"] = {"elements": [1, 2, 5], "nodes": []}
    async with Client(build_server(session)) as client:
        plates = await client.call_tool("lira_find_elements", {"fe_types": [44], "z": [3]})
        shown = await client.call_tool("lira_find_elements", {"within": "visible", "z": [0, 3]})
        bad = await client.call_tool("lira_find_elements", {"z": [1, 2, 3]})
    assert plates.structured_content["elements"] == "5-6"
    assert plates.structured_content["count"] == 2
    assert shown.structured_content["elements"] == "1-2 5"
    assert bad.is_error and "Z min, Z max" in bad.content[0].text
