import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from streamlit.testing.v1 import AppTest
from youth_policy.config import Settings
from youth_policy.storage import write_json
from youth_policy.domain import normalize


class AppTests(unittest.TestCase):
    def test_discovery_without_policy_selection_shows_pending_and_opens_chat(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            settings = Settings(data_dir=Path(directory))
            policy = normalize({"plcyNo": "test", "plcyNm": "가상 취업교육"})
            write_json(settings.data_dir / "policies.json", [policy])
            index = Mock()
            index.meta.count.return_value = index.docs.count.return_value = 1
            report = {"query": "가상 조건", "policy_count": 1, "matches": [], "questions": [],
                      "needs_confirmation": [{"policy": policy, "evidence": "가상 원문",
                                               "verified_conditions": [], "unverified_conditions": ["거주기간 확인"]}]}
            patches.enter_context(patch.dict(os.environ, {"GMS_KEY": "TEST_ONLY", "YOUTH_API_KEY": "TEST_ONLY"}))
            patches.enter_context(patch("youth_policy.config.load_settings", return_value=settings))
            patches.enter_context(patch("youth_policy.llm.Model"))
            patches.enter_context(patch("youth_policy.llm.embeddings"))
            patches.enter_context(patch("youth_policy.index.VectorIndex", return_value=index))
            patches.enter_context(patch("youth_policy.collector.YouthClient"))
            patches.enter_context(patch("youth_policy.sources.SourceCollector"))
            patches.enter_context(patch("youth_policy.rag.build_graph"))
            search = patches.enter_context(patch("youth_policy.discover.explore", return_value=report))
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            self.assertIsNone(app.sidebar.selectbox[0].value)
            app.text_input[0].set_value("가상 조건")
            next(b for b in app.button if b.label == "조건으로 탐색").click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("추가 확인: 거주기간 확인" in str(m.value) for m in app.markdown))
            next(b for b in app.button if b.label == "조건으로 탐색").click().run()
            search.assert_called_once()
            app.text_input[0].set_value("   ")
            next(b for b in app.button if b.label == "조건으로 탐색").click().run()
            search.assert_called_once()
            self.assertTrue(any("탐색할 조건을 입력" in i.value for i in app.info))
            app.text_input[0].set_value("가상 조건")
            next(b for b in app.button if b.label == "조건으로 탐색").click().run()
            next(b for b in app.button if b.label == "이 정책에 질문하기").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.sidebar.selectbox[0].value, "test")
            self.assertEqual(len(app.chat_input), 1)

    def test_policy_conversations_stay_separate_and_checklist_shows_citation(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            settings = Settings(data_dir=Path(directory))
            policies = [normalize({"plcyNo": str(i), "plcyNm": "가상정책 " + str(i)}) for i in range(2)]
            write_json(settings.data_dir / "policies.json", policies)
            source = {"title": "가상 근거", "source_type": "테스트 문서", "collected_at": "2026-10-21",
                      "url": "https://www.youthcenter.go.kr/", "text": "가상 제출서류"}
            index, collector, graph = Mock(), Mock(), Mock()
            index.meta.count.return_value = 2
            index.docs.count.return_value = 2
            graph.invoke.return_value = {"answer": "가상 제출서류 [근거 1]", "sources": [source]}
            patches.enter_context(patch.dict(os.environ, {"GMS_KEY": "TEST_ONLY", "YOUTH_API_KEY": "TEST_ONLY"}))
            patches.enter_context(patch("youth_policy.config.load_settings", return_value=settings))
            patches.enter_context(patch("youth_policy.llm.Model"))
            patches.enter_context(patch("youth_policy.llm.embeddings"))
            patches.enter_context(patch("youth_policy.index.VectorIndex", return_value=index))
            patches.enter_context(patch("youth_policy.collector.YouthClient"))
            patches.enter_context(patch("youth_policy.sources.SourceCollector", return_value=collector))
            patches.enter_context(patch("youth_policy.rag.build_graph", return_value=graph))
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            app.sidebar.selectbox[0].select("0").run()
            next(b for b in app.button if b.label == "신청 준비 체크리스트 만들기").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.chat_message), 2)
            self.assertEqual(len(app.session_state["chats"]["0"]), 2)
            self.assertIn("근거 1", app.chat_message[1].markdown[0].value)
            app.sidebar.selectbox[0].select("1").run()
            self.assertEqual(len(app.chat_message), 0)
            app.sidebar.selectbox[0].select("0").run()
            self.assertEqual(len(app.chat_message), 2)

    def test_empty_data_gives_collection_instructions_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("youth_policy.config.load_settings", return_value=Settings(data_dir=Path(directory))):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            self.assertFalse(app.exception)
            self.assertIn("정책 데이터가 없습니다", app.warning[0].value)

    def test_missing_keys_gives_safe_message(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            write_json(settings.data_dir / "policies.json", [normalize({"plcyNo": "test", "plcyNm": "가상정책"})])
            with patch.dict(os.environ, {"GMS_KEY": "", "LLM_API_KEY": "", "YOUTH_API_KEY": ""}):
                with patch("youth_policy.config.load_settings", return_value=settings):
                    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
            self.assertFalse(app.exception)
            self.assertTrue(any("YOUTH_API_KEY" in item.value for item in app.warning))


if __name__ == "__main__":
    unittest.main()
