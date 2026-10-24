"""실제 데이터·모델로 Streamlit 상호작용을 확인한다. 화면 캡처를 대신하지 않는다."""
from streamlit.testing.v1 import AppTest
from .config import ROOT
from .storage import read_json, write_json
from .domain import now_iso


def run(settings):
    report = read_json(settings.data_dir / "evaluation.json")
    if not report or not report.get("cases"):
        raise ValueError("evaluate 명령을 먼저 실행하세요.")
    case = report["cases"][0]
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    assert not app.exception and not app.error, "앱 초기화 실패"
    app.sidebar.selectbox[0].select(case["policy_id"]).run()
    app.chat_input[0].set_value(case["question"]).run()
    assert not app.exception and not app.error, "실제 질의응답 실패"
    assert len(app.chat_message) == 2, "대화 기록 불일치"
    assert app.session_state["chats"][case["policy_id"]][-1]["sources"], "근거 표시 실패"
    first = case["policy_id"]
    other = next(key for key in app.session_state["chats"] if key != first) if len(app.session_state["chats"]) > 1 else None
    if other is None:
        from .storage import load_policies
        other = next(p["policy_id"] for p in load_policies(settings.data_dir) if p["policy_id"] != first)
    app.sidebar.selectbox[0].select(other).run()
    assert len(app.chat_message) == 0, "정책 간 대화 혼합"
    app.sidebar.selectbox[0].select(first).run()
    assert len(app.chat_message) == 2, "정책 대화 복원 실패"
    next(b for b in app.button if b.label == "신청 준비 체크리스트 만들기").click().run()
    assert not app.exception and not app.error, "체크리스트 실패"
    assert len(app.chat_message) == 4, "체크리스트 대화 기록 불일치"
    assert app.session_state["chats"][first][-1]["sources"], "체크리스트 근거 없음"
    result = {"checked_at": now_iso(), "method": "Streamlit AppTest, 실제 정책 및 GMS",
              "policy_id": first, "question": case["question"], "chat_record_count": len(app.chat_message),
              "policy_history_isolation": True, "history_restore": True, "checklist_with_sources": True,
              "ui_errors": len(app.error), "ui_exceptions": len(app.exception),
              "screenshot_available": False}
    write_json(settings.data_dir / "ui_validation.json", result)
    print("실제 화면 동작 검증 통과: 정책별 대화 분리·복원, 질의응답, 체크리스트, 근거 표시")
    return result
