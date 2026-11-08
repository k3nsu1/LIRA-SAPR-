from lira_sapr_mcp.adapters.analysis import (
    AnalysisState,
    make_event_handler,
    protocol_path,
    summarize_protocol,
)

CLEAN = """\ufeff
Протокол расчета
17:35  Контроль решения
Формирование результатов
Расчет успешно завершен
Затраченное время = 0 мин
"""

UNSTABLE = """Протокол расчета
17:35  Контроль решения
ВНИМАНИЕ!!!!!!! В загружении №1  общая невязка решения 223.607%
ВНИМАНИЕ!!!!!!! Геометрически изменяемая система в узле 1  по направлению UY
Расчет завершен
Затраченное время = 0 мин
"""


def test_clean_protocol():
    s = summarize_protocol(CLEAN)
    assert s["clean"] and s["problems"] == [] and s["verdict"] == "Расчет успешно завершен"


def test_warnings_are_collected():
    s = summarize_protocol(UNSTABLE)
    assert not s["clean"]
    assert s["problem_count"] == 2
    assert "Геометрически изменяемая" in s["problems"][1]


def test_events_drive_the_state():
    state = AnalysisState()
    events = make_event_handler(state)()
    state.start(r"C:\m\frame.lir")
    events.OnAnalysisStarting(0)
    events.OnAnalysisStarting(1001)
    assert state.status == "running"
    events.OnAnalysisFinished(0)
    d = state.as_dict()
    assert d["status"] == "finished"
    assert d["stages"] == ["finite element analysis", "design option 1"]
    assert state.done


def test_dialog_stop_needs_user_and_is_not_done():
    state = AnalysisState()
    state.start("m")
    make_event_handler(state)().OnAnalysisStopped(1)
    assert state.status == "needs_user" and not state.done


def test_protocol_file_name():
    assert protocol_path(r"D:\Work", r"C:\x\frame.lir").name == "frame_01.frame"
