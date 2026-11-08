"""Stamps that tie LIRA's analysis results to the model the server analysed.

LIRA keeps results in its work folder by model name only, so two models of the same name
from different folders share them, and a model saved after its analysis keeps the old
ones. After each analysis it runs, the server writes ``<work>/<name>.lira-mcp.json``: the
model's path, when the results were written and when the model file was saved. Reading
results compares them with the model and explains why the results may not be its own.
LIRA rewrites the model file at the end of the analysis and the same bytes are never
saved twice, so only file times can be compared, not contents.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from lira_sapr_mcp.adapters.base import norm_path

log = logging.getLogger(__name__)

STAMP_SUFFIX = ".lira-mcp.json"
# LIRA saves the model file at the end of its analysis, after the results: a model saved
# within this many seconds of its results counts as the model that was analysed.
SLACK_S = 60.0


def results_marker(work: Path, stem: str) -> Path:
    """The main results file, present whenever LIRA keeps results for this name."""
    return work / f"{stem}#00.{stem}"


def stamp_path(work: Path, stem: str) -> Path:
    return work / f"{stem}{STAMP_SUFFIX}"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _read(work: Path, stem: str) -> dict | None:
    """The stamp, if it describes the results now in the work folder."""
    marker = results_marker(work, stem)
    try:
        stamp = json.loads(stamp_path(work, stem).read_text(encoding="utf-8"))
        if abs(stamp["results_written"] - marker.stat().st_mtime) < 1:
            return stamp
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None  # no stamp, or LIRA analysed a model of this name without the server


def write_stamp(work: Path, model: Path) -> None:
    marker = results_marker(work, model.stem)
    try:
        stamp = {
            "model": str(model),
            "results_written": marker.stat().st_mtime,
            "model_saved": model.stat().st_mtime,
        }
        stamp_path(work, model.stem).write_text(json.dumps(stamp), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not stamp the results of %s: %s", model, exc)


def saved_unchanged(work: Path, model: Path, saved_before: float) -> None:
    """The server saved a model it had only read: if the stamp knew the model as it was
    before the save, keep the stamp valid for the newly saved file."""
    stamp = _read(work, model.stem)
    if (
        stamp
        and norm_path(stamp["model"]) == norm_path(str(model))
        and abs(stamp["model_saved"] - saved_before) < 1
    ):
        stamp["model_saved"] = model.stat().st_mtime
        stamp_path(work, model.stem).write_text(json.dumps(stamp), encoding="utf-8")


def results_rewritten(work: Path, model: Path, written_before: float | None) -> None:
    """The server had LIRA add to the results of a model (the punching shear check
    rewrites the main results file): if the stamp described the results as they were,
    keep it valid for the rewritten file."""
    try:
        stamp = json.loads(stamp_path(work, model.stem).read_text(encoding="utf-8"))
        if (
            written_before is not None
            and norm_path(stamp["model"]) == norm_path(str(model))
            and abs(stamp["results_written"] - written_before) < 1
        ):
            stamp["results_written"] = results_marker(work, model.stem).stat().st_mtime
            stamp_path(work, model.stem).write_text(json.dumps(stamp), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        pass  # no stamp: the results stay as unknown to the server as they were


def punching_loads_file(work: Path, stem: str) -> Path:
    """Loads on punching contours, written by LIRA's punching shear check only."""
    return work / f"{stem}#62.{stem}"


def punching_outdated(work: Path, stem: str) -> bool:
    """Loads on punching contours left by a check run before the last analysis: LIRA's
    analysis does not remove them. Every analysis writes its protocol, the check never."""
    try:
        loads = punching_loads_file(work, stem).stat().st_mtime
        analysed = (work / f"{stem}_01.{stem}").stat().st_mtime
    except OSError:
        return False
    return loads < analysed


def remove_stamp(work: Path, stem: str) -> None:
    stamp_path(work, stem).unlink(missing_ok=True)


def results_warning(work: Path, model: Path) -> str | None:
    """Why the results under this model's name may not be its own, or None."""
    marker = results_marker(work, model.stem)
    if not marker.exists():
        return None
    written = marker.stat().st_mtime
    stamp = _read(work, model.stem)
    if stamp and norm_path(stamp["model"]) != norm_path(str(model)):
        return (
            f"These results are of {stamp['model']}, analysed last under the same name: "
            f"LIRA keeps results by model name only. Run lira_run_analysis for {model} to "
            "get its own results"
        )
    limit = written + SLACK_S
    if stamp and stamp["model_saved"] >= written:  # LIRA's own save was already stamped
        limit = stamp["model_saved"] + 1
    try:
        saved = model.stat().st_mtime
    except OSError:
        return None
    if saved <= limit:
        return None
    return (
        f"{model.name} was saved ({_when(saved)}) after these results were written "
        f"({_when(written)}): if the model changed since, they are out of date"
        + ("" if stamp else ", and they may be of another model of the same name")
        + "; run lira_run_analysis again"
    )
