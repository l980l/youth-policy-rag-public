"""네트워크/유료 모델 없이 검증한다. 정책 데이터는 모두 테스트 전용 가상 값이다."""
import hashlib
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from youth_policy.config import Settings
from youth_policy.collector import YouthClient, YouthAPIError, parse_response
from youth_policy.domain import normalize, audit, policy_document, select_substantive
from youth_policy.rag import build_graph, cited_sources, validate_answer
from youth_policy.sources import SourceCollector
from youth_policy.storage import write_json
from youth_policy.discover import discover


def policy(key="test-1", body="테스트 신청방법 " * 30):
    return normalize({"plcyNo": key, "plcyNm": "테스트용 가상정책 " + key,
                      "plcyAplyMthdCn": body, "plcySprtCn": "테스트 지원내용"})


class APIAndDataTests(unittest.TestCase):
    def test_collect_all_reaches_total_and_keeps_thin_items(self):
        client = YouthClient(Settings(request_interval=0), api_key="TEST_ONLY")
        rows = [policy("one")["raw"], {"plcyNo": "thin", "plcyNm": "가상 목록 항목"}, policy("three")["raw"]]
        def request(**params):
            client.pagination = {"totCount": 3}
            return rows[:2] if params["pageNum"] == 1 else rows[2:]
        client.request = Mock(side_effect=request)
        result = client.collect_all(page_size=2)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[1]["evidence_quality"], "limited")
        self.assertTrue(client.collection_summary["complete"])
        self.assertEqual(client.request.call_count, 2)

    def test_collect_all_repeated_or_premature_empty_page_is_not_complete(self):
        for second in [[policy()["raw"]], []]:
            client = YouthClient(Settings(request_interval=0), api_key="TEST_ONLY")
            client.pagination = {"totCount": 3}
            client.request = Mock(side_effect=[[policy()["raw"]], second])
            with self.assertRaises(YouthAPIError):
                client.collect_all()
            self.assertFalse(client.collection_summary["complete"])

    def test_collect_all_without_total_stops_at_empty_page(self):
        client = YouthClient(Settings(request_interval=0), api_key="TEST_ONLY")
        client.request = Mock(side_effect=[[policy()["raw"]], []])
        self.assertEqual(len(client.collect_all()), 1)
        self.assertTrue(client.collection_summary["complete"])

    def test_collect_all_changed_total_is_not_claimed_as_snapshot(self):
        client = YouthClient(Settings(request_interval=0), api_key="TEST_ONLY")
        def request(**params):
            client.pagination = {"totCount": 3 if params["pageNum"] == 1 else 4}
            return [policy(str(params["pageNum"]))["raw"]]
        client.request = Mock(side_effect=request)
        with self.assertRaises(YouthAPIError):
            client.collect_all()

    def test_quality_selection_excludes_thin_body_and_prioritizes_exclusion(self):
        thin = policy("thin", "")
        detailed = policy("detailed")
        exclusion = policy("exclusion")
        exclusion["raw"]["ptcpPrpTrgtCn"] = "타 사업 참여자 제외"
        selected = select_substantive([thin, detailed, exclusion])
        self.assertEqual([p["policy_id"] for p in selected], ["exclusion", "detailed"])

    def test_auth_error_and_unknown_shape_do_not_become_empty_success(self):
        for value in [{"errorCode": "e001"}, {}, {"result": {"youthPolicyList": "invalid"}}]:
            with self.assertRaises(YouthAPIError):
                parse_response(value)

    def test_repeated_page_stops_and_duplicate_ids_do_not_count_as_100(self):
        client = YouthClient(Settings(request_interval=0), api_key="TEST_ONLY")
        client.request = Mock(return_value=[policy()["raw"]])
        self.assertEqual(len(client.collect(100)), 1)
        self.assertEqual(client.request.call_count, 2)
        self.assertFalse(audit([policy()] * 100)["gate_100_passed"])
        self.assertTrue(audit([policy(str(i)) for i in range(100)])["gate_100_passed"])

    def test_missing_conditions_are_not_invented(self):
        p = normalize({"plcyNo": "test", "plcyNm": "가상정책"})
        self.assertNotIn("제한 없음", policy_document(p)["text"])
        self.assertFalse(audit([p])["gate_100_passed"])

    def test_retry_429_and_redact_connection_exception(self):
        session = Mock()
        limited = Mock(status_code=429, headers={"Retry-After": "0"})
        success = Mock(status_code=200)
        success.json.return_value = {"result": {"youthPolicyList": []}}
        session.get.side_effect = [limited, success]
        client = YouthClient(Settings(request_interval=0), api_key="PRIVATE_TEST_VALUE", session=session)
        with patch("youth_policy.collector.time.sleep"):
            self.assertEqual(client.request(pageNum=1), [])
        self.assertEqual(session.get.call_count, 2)
        import requests
        session.get.side_effect = requests.ConnectionError("url?apiKeyNm=PRIVATE_TEST_VALUE")
        with patch("youth_policy.collector.time.sleep"):
            with self.assertRaises(YouthAPIError) as error:
                client.request()
        self.assertNotIn("PRIVATE_TEST_VALUE", str(error.exception))


class EvidenceTests(unittest.TestCase):
    def test_missing_future_year_is_not_replaced_with_current_application_date(self):
        index, model = Mock(), Mock()
        index.retrieve.return_value = [{"text": "신청 기간: 2026-09-28 ~ 2026-11-20"}]
        graph = build_graph(index, model)
        result = graph.invoke({"policy_id": "test", "question": "2035년에 신청이 시작되는 확정 날짜는?"})
        self.assertIn("2035", result["answer"])
        self.assertNotIn("2026", result["answer"])
        self.assertIn("확인되지", result["answer"])
        model.text.assert_not_called()

    def test_grouped_citations_and_invalid_numbers(self):
        sources = [{"title": str(i)} for i in range(4)]
        self.assertEqual([n for n, _ in cited_sources("내용 [근거 1, 3]", sources)], [1, 3])
        self.assertIn("보류", validate_answer("내용 [근거 5]", sources))
        self.assertIn("확인되지", validate_answer("근거 없이 단정", sources))

    def test_graph_has_two_nodes_and_returns_evidence(self):
        index, model = Mock(), Mock()
        index.retrieve.return_value = [{"title": "테스트 근거", "source_type": "외부 문서",
                                        "text": "조건", "collected_at": "2026-10-21"}]
        model.text.return_value = "테스트 조건 [근거 1]"
        graph = build_graph(index, model)
        result = graph.invoke({"policy_id": "test", "question": "조건은?", "history": []})
        self.assertEqual(result["answer"], "테스트 조건 [근거 1]")
        self.assertEqual(set(graph.get_graph().nodes), {"__start__", "retrieve", "generate", "__end__"})
        index.retrieve.return_value = []
        model.reset_mock()
        self.assertIn("확인되지", graph.invoke({"policy_id": "test", "question": "?"})["answer"])
        model.text.assert_not_called()

    def test_selected_document_survives_rejection_and_repeat_uses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            client, model, index = Mock(), Mock(), Mock()
            selected = policy()
            client.detail.return_value = selected
            client.search.return_value = [selected["raw"]]
            model.json.side_effect = lambda prompt, payload: {"queries": []} if "queries" in prompt else {"ids": []}
            collector = SourceCollector(settings, client, model, index)
            collector.ensure(selected, "조건은?")
            collector.ensure(selected, "조건은?")
            self.assertEqual(client.detail.call_count, 1)
            self.assertEqual(client.search.call_count, 1)
            self.assertEqual(index.index_document.call_count, 2)
            self.assertEqual(index.index_document.call_args.args[0], selected["policy_id"])

    def test_failed_index_does_not_mark_selection_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            client, model, index = Mock(), Mock(), Mock()
            p, other = policy(), policy("test-2")
            client.detail.side_effect = lambda key: p if key == p["policy_id"] else other
            client.search.return_value = [other["raw"]]
            model.json.side_effect = [{"queries": []}, {"ids": [other["policy_id"]]}]
            index.index_document.side_effect = [None, RuntimeError("offline")]
            with self.assertRaises(RuntimeError):
                SourceCollector(settings, client, model, index).ensure(p, "조건")
            self.assertEqual(list(Path(directory).glob("selection_cache/*.json")), [])

    def test_optional_search_failure_keeps_existing_evidence_and_does_not_cache_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            client, model, index = Mock(), Mock(), Mock()
            p = policy()
            client.detail.return_value = p
            client.search.side_effect = YouthAPIError("테스트용 연결 실패")
            model.json.return_value = {"queries": []}
            collector = SourceCollector(settings, client, model, index)
            trace = collector.ensure(p, "조건")
            self.assertEqual(trace[0]["status"], "external_search_unavailable")
            index.index_document.assert_called_once()
            self.assertEqual(list(Path(directory).glob("search_cache/*.json")), [])
            collector.ensure(p, "조건")
            self.assertEqual(model.json.call_count, 1)
            self.assertEqual(client.search.call_count, 2)

    def test_discover_rejects_invented_quotes_and_ids(self):
        p = policy()
        index, model = Mock(), Mock()
        index.candidates.return_value = [{"policy_id": p["policy_id"]}]
        model.json.return_value = {"matches": [{"id": p["policy_id"], "evidence": "허구의 자격조건"},
                                               {"id": "unknown", "evidence": "테스트"}]}
        self.assertEqual(discover("조건", [p], index, model), [])


class IndexTests(unittest.TestCase):
    def test_bulk_index_is_bounded_and_idempotent(self):
        from youth_policy.index import VectorIndex
        class LocalEmbeddings:
            calls = 0
            largest = 0
            def embed_documents(self, texts):
                self.calls += 1
                self.largest = max(self.largest, len(texts))
                return [[1.0, 0.5, 0.1] for _ in texts]
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            embedder = LocalEmbeddings()
            index = VectorIndex(Settings(data_dir=Path(directory)), embedder)
            cleanup.callback(index.client.close)
            policies = [policy(str(i)) for i in range(257)]
            index.index_policies(policies)
            index.index_documents((p["policy_id"], policy_document(p)) for p in policies)
            counts = (index.meta.count(), index.docs.count(), embedder.calls)
            self.assertEqual(counts[0], 257)
            self.assertGreater(counts[1], 512)
            self.assertLessEqual(embedder.largest, 64)
            index.index_policies(policies)
            index.index_documents((p["policy_id"], policy_document(p)) for p in policies)
            self.assertEqual(counts, (index.meta.count(), index.docs.count(), embedder.calls))

    def test_exclusion_header_is_preserved_on_every_split(self):
        from youth_policy.index import split_document
        p = policy()
        p["raw"]["ptcpPrpTrgtCn"] = "재직 중인 자는 참여 불가. " * 80
        chunks = split_document(policy_document(p), 180, 30)
        restricted = [c for c in chunks if "재직 중" in c]
        self.assertGreater(len(restricted), 1)
        self.assertTrue(all("문서 구분: 참여 제한 및 제외조건" in c for c in restricted))
        self.assertTrue(all(len(c) <= 180 for c in chunks))

    def test_persistent_reindex_and_shortened_document_remove_old_chunks(self):
        from youth_policy.index import VectorIndex

        class TestEmbeddings:
            calls = 0

            def embed_documents(self, texts):
                self.calls += len(texts)
                return [[1.0, float(len(t) % 17 + 1), 0.5] for t in texts]

            def embed_query(self, text):
                return [1.0, 1.0, 0.5]

        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            settings = Settings(data_dir=Path(directory), chunk_size=100, chunk_overlap=20)
            embedder = TestEmbeddings()
            index = VectorIndex(settings, embedder)
            cleanup.callback(index.client.close)
            p = policy(body="가상 신청 절차. " * 100)
            doc = policy_document(p)
            index.index_policies([p])
            index.index_document(p["policy_id"], doc)
            counts = (index.meta.count(), index.docs.count(), embedder.calls)
            index.index_policies([p])
            index.index_document(p["policy_id"], doc)
            self.assertEqual(counts, (index.meta.count(), index.docs.count(), embedder.calls))
            doc["collected_at"] = "2026-10-22T10:00:00+09:00"
            index.index_document(p["policy_id"], doc)
            self.assertEqual(embedder.calls, counts[2])
            self.assertTrue(all(m["collected_at"] == doc["collected_at"]
                                for m in index.docs.get(include=["metadatas"])["metadatas"]))
            self.assertGreater(index.docs.count(), 1)
            doc["text"] = "테스트용 짧은 공식 근거"
            index.index_document(p["policy_id"], doc)
            self.assertEqual(index.docs.count(), 1)
            sources = index.retrieve(p["policy_id"], "질문")
            self.assertEqual(sources[0]["source_type"], "항목 메타데이터")
            self.assertTrue(all(s["policy_id"] == p["policy_id"] for s in sources))
            # 다른 정책에서 같은 문서를 사용해도 기존 연결을 덮어쓰지 않는다.
            index.index_document("another-policy", doc)
            self.assertEqual(index.docs.count(), 2)
            with self.assertRaises(ValueError):
                VectorIndex(Settings(data_dir=Path(directory), chunk_size=200), embedder)


if __name__ == "__main__":
    unittest.main()
