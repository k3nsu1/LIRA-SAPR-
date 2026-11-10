"""K2 probes use only test copies of installed tutorial models."""

import json
import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.lira


def test_dcl_input_table_is_read_on_a_test_copy(lira, adapter, workdir):
    root = Path(r"C:\Users\Public\Documents\LIRA SAPR\LIRA SAPR 2024\Manual\RU")
    sources = list(root.glob("04*/*.lir"))
    assert sources, "Tutorial 04 is required for the DCL probe"
    path = str(workdir / "lmcp_k2_dcl.lir")
    shutil.copyfile(sources[0], path)
    lira(adapter.open_document, path)
    lira(adapter.start_analysis, path)
    deadline = time.monotonic() + 180
    while True:
        status = lira(adapter.analysis_status)
        if status["status"] != "running":
            break
        assert time.monotonic() < deadline, status
        time.sleep(0.25)
    print("ANALYSIS=" + json.dumps(status, ensure_ascii=True))
    print("COMBINATIONS=" + json.dumps(lira(adapter.load_combinations, path), ensure_ascii=True))
    lira(adapter.open_document, path)
    rows = lira(adapter.read_table, path, "DCL")
    print("DCL_INPUT=" + json.dumps(rows, ensure_ascii=True))
    assert rows and len(rows) > 1
