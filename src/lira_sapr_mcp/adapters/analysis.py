"""Analysis state fed by LIRA's COM events, and reading the analysis protocol.

LIRA reports the analysis through events of the application object: ``AnalysisStarting``
(stage), ``AnalysisFinished`` (code) and ``AnalysisStopped`` (code; 1 means a dialog waits
for the user). They arrive while the COM thread pumps messages.

The solver writes its protocol to ``<work folder>/<model>_01.<model>``. A finished
analysis can still carry problems (e.g. a geometrically unstable structure); they are the
protocol lines starting with "ВНИМАНИЕ" or "ОШИБКА".
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from lira_sapr_mcp.adapters.base import decode_lira_text

STAGES = {
    0: "finite element analysis",
    1: "design combinations of forces (DCF)",
    2: "DCF for unified combinations",
    3: "loads on fragment",
    4: "design combinations of loads (DCL)",
    5: "stability",
    6: "stresses",
    7: "cracks",
    100000: "CSV result tables",
}
FINISH_CODES = {0: "ok", 1: "failed", 2: "stopped"}
STOP_CODES = {0: "stopped in FE analysis", 1: "a LIRA dialog waits for the user"}
_PROBLEM_MARKS = ("ВНИМАНИЕ", "ОШИБКА", "ERROR", "WARNING")


@dataclass
class AnalysisState:
    model: str | None = None
    status: str = "idle"  # idle | running | finished | failed | stopped | needs_user
    stages: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None

    def start(self, model: str) -> None:
        self.model, self.status, self.stages = model, "running", []
        self.started_at, self.finished_at = time.time(), None

    def on_stage(self, stage: int) -> None:
        self.stages.append(
            STAGES.get(stage, f"design option {stage - 1000}" if stage > 1000 else str(stage))
        )

    def on_finished(self, code: int) -> None:
        self.status = {0: "finished", 2: "stopped"}.get(code, "failed")
        self.finished_at = time.time()

    def on_stopped(self, code: int) -> None:
        self.status = "needs_user" if code == 1 else "stopped"
        if code != 1:
            self.finished_at = time.time()

    @property
    def done(self) -> bool:
        return self.status in ("finished", "failed", "stopped")

    def as_dict(self) -> dict:
        end = self.finished_at or time.time()
        elapsed = round(end - self.started_at, 1) if self.started_at else None
        return {
            "model": self.model,
            "status": self.status,
            "stages": list(self.stages),
            "elapsed_s": elapsed,
        }


def make_event_handler(state: AnalysisState) -> type:
    """Event sink class for ``win32com.client.WithEvents`` that updates ``state``."""

    class LiraEvents:
        def OnAnalysisStarting(self, Stage: int) -> None:
            state.on_stage(int(Stage))

        def OnAnalysisFinished(self, FinishCode: int) -> None:
            state.on_finished(int(FinishCode))

        def OnAnalysisStopped(self, StopCode: int) -> None:
            state.on_stopped(int(StopCode))

    return LiraEvents


def protocol_path(work_dir: str, model_path: str) -> Path:
    stem = Path(model_path).stem
    return Path(work_dir) / f"{stem}_01.{stem}"


def read_protocol(path: Path) -> str | None:
    if not path.exists():
        return None
    raw = path.read_bytes()
    text = decode_lira_text(raw)
    return text if text is not None else raw.decode("utf-8", errors="replace")


def summarize_protocol(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    problems = [ln for ln in lines if ln.upper().startswith(_PROBLEM_MARKS)]
    verdict = next((ln for ln in reversed(lines) if ln.lower().startswith("расчет")), None)
    return {
        "verdict": verdict,
        "clean": verdict is not None and "успешно" in verdict.lower() and not problems,
        "problems": problems[:50],
        "problem_count": len(problems),
    }
