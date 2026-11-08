"""Backups: listed newest first, restored in place, and the restore can be undone."""

import pytest
from mcp import Client

from fakes import fake_session
from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.backups import Backups

pytestmark = pytest.mark.anyio


def model(tmp_path, content=b"v1"):
    path = tmp_path / "frame.lir"
    path.write_bytes(content)
    return path


async def test_backups_of_a_model_newest_first_and_only_its_own(tmp_path):
    path = model(tmp_path)
    backups = Backups("lira-mcp-backups")
    first = backups.snapshot(path)
    path.write_bytes(b"v2")
    second = backups.snapshot(path)  # the same second: gets a -2 suffix
    folder = tmp_path / "lira-mcp-backups"
    (folder / "frame_2_20260101-120000.lir").write_bytes(b"another model")
    (folder / "frame_notes.lir").write_bytes(b"not a backup")
    listed = backups.of(str(path))
    assert [b["path"] for b in listed] == [str(second), str(first)]
    assert listed[0]["bytes"] == 2 and listed[0]["made"].startswith("20")


async def test_a_model_is_backed_up_once_a_session_or_before_every_rebuild(tmp_path):
    path = model(tmp_path)
    backups = Backups("lira-mcp-backups")
    first = backups.ensure(str(path))
    path.write_bytes(b"v2")
    assert backups.ensure(str(path)) == first  # small edits: the state before the first
    again = backups.ensure(str(path), fresh=True)  # a load change rewrites the whole file
    assert again != first and again.read_bytes() == b"v2" and first.read_bytes() == b"v1"


@pytest.mark.parametrize("pack", [False, True])
async def test_every_load_change_gets_its_own_backup(tmp_path, pack):
    session, adapter = fake_session(tmp_path)
    path = tmp_path / "frame.lir"
    path.write_bytes(b"original")
    adapter.open_document(str(path))
    calls = []
    adapter.numbering_gaps = lambda ref: {}

    def set_loads(adapter_, ref, loads, cases, pack_, targets=None):
        calls.append(pack_)
        return {"model": ref}

    import lira_sapr_mcp.services.loads as loads_service

    original = loads_service.set_loads
    loads_service.set_loads = set_loads
    try:
        async with Client(build_server(session)) as client:
            load = {"case": 1, "target": 3, "type": 6, "direction": 3, "values": [1]}
            args = {"loads": [load], "load_cases": [1], "pack": pack}
            first = (await client.call_tool("lira_set_loads", args)).structured_content
            second = (await client.call_tool("lira_set_loads", args)).structured_content
    finally:
        loads_service.set_loads = original
    assert first["backup"] != second["backup"] and calls == [pack, pack]


async def test_restore_puts_a_backup_back_and_can_be_undone(tmp_path):
    path = model(tmp_path)
    backups = Backups("lira-mcp-backups")
    original = backups.snapshot(path)
    path.write_bytes(b"broken by the agent")
    done = backups.restore(str(path), backups.choose(str(path), str(original)))
    assert path.read_bytes() == b"v1"
    assert done["restored_from"] == str(original)
    # The newest backup is now the broken version: restoring it undoes the restore.
    backups.restore(str(path), backups.choose(str(path)))
    assert path.read_bytes() == b"broken by the agent"


async def test_restore_takes_only_backups_of_this_model(tmp_path):
    path = model(tmp_path)
    backups = Backups("lira-mcp-backups")
    with pytest.raises(LiraInputError, match="No backups"):
        backups.choose(str(path))
    stranger = tmp_path / "other.lir"
    stranger.write_bytes(b"x")
    with pytest.raises(LiraInputError, match="is not a backup"):
        backups.choose(str(path), str(stranger))


async def test_restore_tool_closes_and_reopens_an_open_model(tmp_path):
    session, adapter = fake_session(tmp_path)
    path = model(tmp_path)
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_open_model", {"path": str(path)})
        await client.call_tool(
            "lira_write_table", {"table": "Nodes_Coordinates", "rows": [[1, 0, 0, 0]]}
        )  # backs up v1 and leaves unsaved changes
        refused = await client.call_tool("lira_restore_backup", {})
        listed = await client.call_tool("lira_list_backups", {"model": str(path)})
        path.write_bytes(b"v2 saved by the agent")
        done = await client.call_tool("lira_restore_backup", {"discard_changes": True})
    assert refused.is_error and "unsaved changes" in refused.content[0].text
    assert len(listed.structured_content["backups"]) == 1
    assert not done.is_error, done.content
    assert done.structured_content["reopened"] is True
    assert path.read_bytes() == b"v1"
    assert str(path) in adapter.docs  # open again


async def test_a_wrong_backup_leaves_an_open_model_open_and_unchanged(tmp_path):
    session, adapter = fake_session(tmp_path)
    path = model(tmp_path)
    stranger = tmp_path / "other.lir"
    stranger.write_bytes(b"x")
    async with Client(build_server(session)) as client:
        await client.call_tool("lira_open_model", {"path": str(path)})
        await client.call_tool(
            "lira_write_table", {"table": "Nodes_Coordinates", "rows": [[1, 0, 0, 0]]}
        )
        refused = await client.call_tool(
            "lira_restore_backup", {"backup": str(stranger), "discard_changes": True}
        )
    assert refused.is_error and "is not a backup" in refused.content[0].text
    assert str(path) in adapter.docs and adapter.docs[str(path)]["modified"]
    assert path.read_bytes() == b"v1"
