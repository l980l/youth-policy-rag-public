import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from youth_policy.web import PolicyService, create_server, policy_summary


class FakeService:
    def __init__(self):
        self.list_calls = []
        self.chat_calls = []

    def bootstrap(self):
        return {"stats": {"policy_count": 2, "metadata_count": 2, "document_chunk_count": 4,
                          "collected_at": "2026-10-24T10:00:00+09:00"},
                "categories": [{"name": "일자리", "count": 1}, {"name": "주거", "count": 1}]}

    def list_policies(self, query, category, page, limit):
        self.list_calls.append((query, category, page, limit))
        return {"items": [], "total": 0, "page": page, "limit": limit, "total_pages": 0}

    def detail(self, policy_id):
        if policy_id == "missing":
            from youth_policy.web import NotFoundError
            raise NotFoundError("정책을 찾을 수 없습니다.")
        return {"policy": {"policy_id": policy_id, "name": "테스트 정책"}, "sections": []}

    def discover(self, query):
        if query == "provider-fails":
            raise RuntimeError("https://provider.example/?key=never-expose")
        return {"query": query, "policy_count": 2, "matches": [], "needs_confirmation": [],
                "questions": [], "seconds": 0.01}

    def chat(self, policy_id, question, history):
        self.chat_calls.append((policy_id, question, history))
        return {"answer": "확인된 내용입니다. [근거 3]", "sources": [{"number": 3, "title": "공식 문서",
                "url": "https://example.com/source", "text": "근거", "source_type": "테스트",
                "collected_at": "2026-10-24"}], "seconds": 0.01, "partial_sources": False}


class WebHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        static = Path(self.tmp.name)
        (static / "index.html").write_text("<h1>안전한 화면</h1>", encoding="utf-8")
        (static / "styles.css").write_text("body{}", encoding="utf-8")
        self.service = FakeService()
        self.server = create_server(self.service, static, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, body=None):
        request = Request(self.base + path, data=body, method="POST" if body is not None else "GET")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_bootstrap_listing_detail_and_static_file_are_real_http(self):
        status, headers, body = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["stats"]["policy_count"], 2)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

        status, _, body = self.request("/api/policies?" + urlencode({"q": "청년", "category": "일자리", "page": 2, "limit": 8}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["page"], 2)
        self.assertEqual(self.service.list_calls[-1], ("청년", "일자리", 2, 8))

        status, _, body = self.request("/api/policies/p-100")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["policy"]["policy_id"], "p-100")

        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("안전한 화면", body.decode())

    def test_posts_validate_history_and_keep_source_numbers(self):
        payload = json.dumps({"policy_id": "p-1", "question": "무엇을 준비하나요?", "history": [
            {"role": "user", "content": "지난 질문"}, {"role": "assistant", "content": "지난 답변"},
        ]}, ensure_ascii=False).encode()
        status, _, body = self.request("/api/chat", payload)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["sources"][0]["number"], 3)
        self.assertEqual(self.service.chat_calls[-1][2][0]["role"], "user")

        bad = json.dumps({"policy_id": "p-1", "question": "x", "history": [{"role": "system", "content": "x"}]}).encode()
        status, _, body = self.request("/api/chat", bad)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

        status, _, body = self.request("/api/discover", b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

    def test_errors_and_static_boundaries_do_not_leak_secrets(self):
        status, _, body = self.request("/api/policies/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

        status, _, body = self.request("/api/discover", json.dumps({"query": "provider-fails"}).encode())
        self.assertEqual(status, 503)
        self.assertNotIn("provider.example", body.decode())
        self.assertNotIn("never-expose", body.decode())

        for path in ("/.env", "/data/policies.json", "/youth_policy/web.py", "/%2e%2e/.env"):
            status, _, body = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_cross_origin_simple_posts_cannot_trigger_model_calls(self):
        payload = json.dumps({"policy_id": "p-1", "question": "질문", "history": []}).encode()
        request = Request(self.base + "/api/chat", data=payload, method="POST",
                          headers={"Content-Type": "text/plain", "Origin": "https://unrelated.example"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.service.chat_calls, [])


class SummaryTests(unittest.TestCase):
    def test_primary_category_summary_is_conservative_and_uses_source_values(self):
        policy = {"policy_id": "one", "name": "청년 직업 훈련", "category": "일자리", "subcategory": "직업교육",
                  "source_url": "https://official.example/policy", "collected_at": "2026-10-24", "evidence_quality": "limited",
                  "raw": {"plcyExplnCn": "<b>공식</b> 지원 설명", "sprvsnInstCdNm": "청년 기관",
                          "sprtTrgtMinAge": "19", "sprtTrgtMaxAge": "34"}}
        result = policy_summary(policy)
        self.assertEqual(result["category"], "일자리")
        self.assertEqual(result["summary"], "공식 지원 설명")
        self.assertEqual(result["institution"], "청년 기관")
        self.assertEqual(result["age_label"], "만 19~34세")

        unknown_age = {**policy, "raw": {"plcySprtCn": "지원 내용", "sprtTrgtAgeLmtYn": "N"}}
        self.assertEqual(policy_summary(unknown_age)["age_label"], "")

        education = {**policy, "category": "교육", "subcategory": "직업훈련"}
        self.assertEqual(policy_summary(education)["category"], "교육")
        invalid_age = {**policy, "raw": {"sprtTrgtMinAge": "0", "sprtTrgtMaxAge": "0"}}
        self.assertEqual(policy_summary(invalid_age)["age_label"], "")
        reversed_age = {**policy, "raw": {"sprtTrgtMinAge": "35", "sprtTrgtMaxAge": "19"}}
        self.assertEqual(policy_summary(reversed_age)["age_label"], "")


class ChatFormatRecoveryTests(unittest.TestCase):
    def test_missing_citation_format_retries_once_and_returns_only_cited_sources(self):
        from unittest.mock import Mock, patch
        source = {"title": "공식 근거", "url": "https://example.com/source", "text": "지원 원문",
                  "source_type": "공식 API", "collected_at": "2026-10-24"}
        service = PolicyService(policies=[{"policy_id": "test"}])
        collector, graph = Mock(), Mock()
        collector.ensure.return_value = []
        graph.invoke.side_effect = [
            {"answer": "문서에서 확인되지 않습니다. 질문을 뒷받침하는 유효한 근거 인용이 없습니다.", "sources": [source]},
            {"answer": "지원 내용입니다. [근거 1]", "sources": [source]},
        ]
        with patch.object(service, "_runtime", return_value=(None, None, collector, graph)):
            result = service.chat("test", "지원 내용은?", [])
        self.assertEqual(graph.invoke.call_count, 2)
        collector.ensure.assert_called_once()
        self.assertEqual(result["sources"][0]["number"], 1)

    def test_unknown_future_date_is_not_retried_as_a_format_failure(self):
        from unittest.mock import Mock, patch
        service = PolicyService(policies=[{"policy_id": "test"}])
        collector, graph = Mock(), Mock()
        collector.ensure.return_value = []
        graph.invoke.return_value = {"answer": "문서에서 2035년의 신청 일정은 확인되지 않습니다.", "sources": [{"text": "2026년"}]}
        with patch.object(service, "_runtime", return_value=(None, None, collector, graph)):
            result = service.chat("test", "2035년 신청일은?", [])
        self.assertEqual(graph.invoke.call_count, 1)
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
