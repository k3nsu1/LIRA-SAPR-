"""Stamps that tell whether LIRA's results under a model's name are that model's own."""

import json
import os

import pytest

from lira_sapr_mcp.adapters import stamps

T0 = 1_800_000_000.0  # when LIRA wrote the results


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "Work"
    folder.mkdir()
    return folder


def file_at(path, ts):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (ts, ts))
    return path


def analysed(work, model, model_saved=T0 + 0.3):
    """As after an analysis: results written, then LIRA saves the model file."""
    file_at(stamps.results_marker(work, model.stem), T0)
    file_at(model, model_saved)


def test_results_just_written_by_the_servers_analysis_are_trusted(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)

    assert stamps.results_warning(work, model) is None


def test_lira_saving_the_model_after_the_stamp_is_allowed(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model, model_saved=T0 - 10)
    stamps.write_stamp(work, model)  # before LIRA's own save at the end of the analysis
    os.utime(model, (T0 + 0.5, T0 + 0.5))

    assert stamps.results_warning(work, model) is None


def test_a_same_named_model_analysed_last_is_named(work, tmp_path):
    a, b = tmp_path / "a" / "Frame.lir", tmp_path / "b" / "Frame.lir"
    file_at(a, T0 - 3600)
    analysed(work, b)
    stamps.write_stamp(work, b)

    warning = stamps.results_warning(work, a)

    assert warning and str(b) in warning and "same name" in warning
    assert stamps.results_warning(work, b) is None


def test_a_model_saved_after_its_analysis_gets_a_warning(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)
    os.utime(model, (T0 + 5, T0 + 5))  # e.g. the agent changed a section and saved

    warning = stamps.results_warning(work, model)

    assert warning and "out of date" in warning and "another model" not in warning


def test_a_save_of_a_model_the_server_only_read_keeps_the_results_trusted(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)
    before = model.stat().st_mtime
    os.utime(model, (T0 + 600, T0 + 600))

    stamps.saved_unchanged(work, model, before)

    assert stamps.results_warning(work, model) is None


def test_a_check_that_rewrites_the_results_keeps_the_stamp(work, tmp_path):
    a, b = tmp_path / "a" / "Frame.lir", tmp_path / "b" / "Frame.lir"
    file_at(a, T0 - 3600)
    analysed(work, b)
    stamps.write_stamp(work, b)
    os.utime(stamps.results_marker(work, "Frame"), (T0 + 120, T0 + 120))  # punching check

    stamps.results_rewritten(work, b, T0)

    assert str(b) in stamps.results_warning(work, a)
    assert stamps.results_warning(work, b) is None


def test_a_rewrite_does_not_revive_a_stamp_of_other_results(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)
    os.utime(stamps.results_marker(work, "Frame"), (T0 + 7200, T0 + 7200))  # LIRA's analysis

    stamps.results_rewritten(work, model, T0 + 7200)  # the check ran on those new results

    stamp = json.loads(stamps.stamp_path(work, "Frame").read_text(encoding="utf-8"))
    assert stamp["results_written"] == T0  # still describes the old results only


def test_punching_loads_older_than_the_last_analysis_are_outdated(work):
    loads = file_at(stamps.punching_loads_file(work, "Slab"), T0 + 60)  # the check
    protocol = file_at(work / "Slab_01.Slab", T0)  # the analysis

    assert not stamps.punching_outdated(work, "Slab")
    os.utime(protocol, (T0 + 600, T0 + 600))  # analysed again: LIRA keeps the old loads
    assert stamps.punching_outdated(work, "Slab")
    loads.unlink()
    assert not stamps.punching_outdated(work, "Slab")  # no loads: nothing to be stale


def test_a_save_after_a_real_change_stays_suspicious(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)
    os.utime(model, (T0 + 300, T0 + 300))  # changed and saved by the agent
    before = model.stat().st_mtime
    os.utime(model, (T0 + 600, T0 + 600))  # then read and saved again by the server

    stamps.saved_unchanged(work, model, before)

    assert "out of date" in stamps.results_warning(work, model)


def test_results_of_an_analysis_run_in_lira_itself(work, tmp_path):
    """No stamp: only the file times can tell, with LIRA's own save at the end allowed."""
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model, model_saved=T0 + 20)
    assert stamps.results_warning(work, model) is None

    os.utime(model, (T0 + 3600, T0 + 3600))
    warning = stamps.results_warning(work, model)
    assert "out of date" in warning and "another model of the same name" in warning


def test_a_stamp_of_older_results_is_ignored(work, tmp_path):
    """LIRA analysed a model of this name again without the server: the old stamp no
    longer describes the results."""
    a, b = tmp_path / "a" / "Frame.lir", tmp_path / "b" / "Frame.lir"
    analysed(work, b)
    stamps.write_stamp(work, b)
    file_at(stamps.results_marker(work, "Frame"), T0 + 7200)
    file_at(a, T0 + 7200)

    assert stamps.results_warning(work, a) is None


def test_no_results_no_warning(work, tmp_path):
    model = file_at(tmp_path / "Frame.lir", T0)
    assert stamps.results_warning(work, model) is None


def test_removed_stamp(work, tmp_path):
    model = tmp_path / "a" / "Frame.lir"
    analysed(work, model)
    stamps.write_stamp(work, model)
    stamps.remove_stamp(work, model.stem)
    assert not stamps.stamp_path(work, model.stem).exists()
