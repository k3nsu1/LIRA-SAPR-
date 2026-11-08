import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import parse_numbers
from lira_sapr_mcp.server import build_server


def test_number_lists_accept_lira_ranges():
    assert parse_numbers("1-3 7, 10-11") == [1, 2, 3, 7, 10, 11]
    assert parse_numbers([4, 5]) == [4, 5]
    assert parse_numbers(None) is None
    assert parse_numbers("") is None


@pytest.mark.parametrize("bad", ["3-1", "a-b", "1;2"])
def test_bad_number_lists_are_rejected(bad):
    with pytest.raises(LiraInputError):
        parse_numbers(bad)


@pytest.mark.anyio
async def test_write_then_read_pages(tmp_path):
    session, _ = fake_session(tmp_path)
    model = tmp_path / "m.lir"
    model.write_bytes(b"x")
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_open_model", {"path": str(model)})
        rows = [[i, i, 0, 0] for i in range(1, 6)]
        res = await client.call_tool(
            "lira_write_table", {"table": "Nodes_Coordinates", "rows": rows}
        )
        assert res.structured_content["applied"]
        assert res.structured_content["backup"]
        page = await client.call_tool(
            "lira_read_table", {"table": "Nodes_Coordinates", "offset": 3, "limit": 10}
        )
    assert page.structured_content["total_rows"] == 5
    assert page.structured_content["rows"] == rows[3:]
