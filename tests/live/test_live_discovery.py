"""Live checks: need a licensed LIRA-SAPR 2024 R2+ on this computer (run with -m lira)."""

import pytest

from lira_sapr_mcp.adapters.discovery import find_installs, pick_install
from lira_sapr_mcp.com.worker import ComWorker

pytestmark = pytest.mark.lira


def test_finds_a_supported_lira():
    install = pick_install(find_installs(), None)
    assert install is not None
    assert install.year >= 2024
    assert install.exe.lower().endswith("lirasapr.exe")


def test_connects_through_the_com_worker():
    install = pick_install(find_installs(), None)

    def connect():
        from win32com.client import gencache

        app = gencache.EnsureDispatch(install.progid)
        return app.DocumentCount

    worker = ComWorker()
    try:
        assert worker.call(connect, timeout=120) >= 0
    finally:
        worker.stop()
