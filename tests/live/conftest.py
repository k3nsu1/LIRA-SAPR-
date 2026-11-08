"""Shared fixtures for tests against a real LIRA. Models live only in tests/live/_work."""

import shutil
from pathlib import Path

import pytest

from lira_sapr_mcp.adapters.com_adapter import ComLiraAdapter
from lira_sapr_mcp.adapters.discovery import find_installs, lira_directories, pick_install
from lira_sapr_mcp.com.worker import ComWorker
from lira_sapr_mcp.config import Settings
from lira_sapr_mcp.server import build_server
from lira_sapr_mcp.services.session import LiraSession

WORK = Path(__file__).parent / "_work"
# Every model a live test makes is named so; LIRA keeps their texts, stages and analysis
# results in its own data and work folders under the same names.
PREFIX = "lmcp_"


@pytest.fixture(scope="session", autouse=True)
def no_leftovers_in_lira_folders():
    """After the run, remove what LIRA wrote for the test models into its own folders:
    the owner works there. Only entries made during this run go: probes and other
    sessions name their models lmcp_ too, and their analysis results must stay."""
    install = pick_install(find_installs(), None)
    if install is None:
        yield
        return
    dirs = lira_directories(install.year)
    folders = [
        Path(dirs[key])
        for key in ("TextDataDirectory", "DataDirectory", "WorkDirectory")
        if dirs.get(key) and Path(dirs[key]).is_dir()
    ]
    before = {entry for folder in folders for entry in folder.iterdir()}
    yield
    for folder in folders:
        for entry in folder.iterdir():
            if not entry.name.lower().startswith(PREFIX) or entry in before:
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:  # still held by LIRA; the next run removes it
                pass


@pytest.fixture(scope="session")
def worker():
    w = ComWorker()
    yield w
    w.stop()


@pytest.fixture(scope="session")
def adapter(worker):
    install = pick_install(find_installs(), None)
    if install is None:
        pytest.skip("no LIRA 2024+ installed")
    a = ComLiraAdapter(install)
    worker.call(a.connect, timeout=180)
    yield a
    worker.call(a.disconnect, timeout=30)


@pytest.fixture
def lira(worker, adapter):
    """Call adapter methods on the COM thread: ``lira(adapter.method, *args)``."""

    def run(fn, *args, **kwargs):
        return worker.call(fn, *args, timeout=180, **kwargs)

    # Close only what the test opened: the owner may have own models open in LIRA.
    before = {d.path.lower() for d in run(adapter.list_documents)}
    yield run
    for doc in run(adapter.list_documents):
        if doc.path.lower() not in before:
            run(adapter.close_document, doc.path or doc.title, True)


@pytest.fixture
def workdir(request):
    d = WORK / request.node.name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mcp(tmp_path, worker, adapter):
    """A server with its own session; afterwards every model the test left open in
    tests/live/_work is closed without saving, or the next run would find it open."""
    session = LiraSession(Settings(log_file=tmp_path / "log.txt"))
    yield build_server(session)
    session.close()
    for doc in worker.call(adapter.list_documents, timeout=60):
        if doc.path and Path(doc.path).resolve().is_relative_to(WORK.resolve()):
            worker.call(adapter.close_document, doc.path, True, timeout=60)
