import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from youth_policy.serverless import make_serverless_handler
from youth_policy.usage import HostedQuotaError


class FakeService:
    def __init__(self):
        self.list_calls = []
        self.discover_calls = []
        self.chat_calls = []

    def bootstrap(self):
        return {"ready": True}

    def list_policies(self, query, category, page, limit):
        self.list_calls.append((query, category, page, limit))
        return {"items": [], "total": 0, "page": page, "limit": limit, "total_pages": 0}

    def detail(self, policy_id):
        return {"policy": {"policy_id": policy_id}, "sections": []}

    def discover(self, query):
        self.discover_calls.append(query)
        return {"query": query, "matches": [], "needs_confirmation": [], "questions": []}

    def chat(self, policy_id, question, history):
        self.chat_calls.append((policy_id, question, history))
        return {"answer": "확인했습니다.", "sources": []}


class ServerlessHTTPTests(unittest.TestCase):
    def setUp(self):
        self.factory_calls = 0
        self.service = FakeService()

        def factory():
            self.factory_calls += 1
            return self.service

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_serverless_handler(factory))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, path, payload=None):
        request = Request(self.base_url + path, data=payload, method="POST" if payload is not None else "GET")
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_health_does_not_initialize_service_and_api_initializes_once(self):
        status, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(self.factory_calls, 0)

        status, body = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])
        self.assertEqual(self.factory_calls, 1)

        status, _ = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(self.factory_calls, 1)

    def test_query_and_post_routes_keep_original_api_contract(self):
        status, body = self.request("/api/policies?" + urlencode({"q": "청년", "category": "주거", "page": 2, "limit": 8}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["page"], 2)
        self.assertEqual(self.service.list_calls, [("청년", "주거", 2, 8)])

        status, body = self.request("/api/policies/p-1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["policy"]["policy_id"], "p-1")

        status, _ = self.request("/api/discover", json.dumps({"query": "서울 청년 주거"}, ensure_ascii=False).encode())
        self.assertEqual(status, 200)
        self.assertEqual(self.service.discover_calls, ["서울 청년 주거"])

        payload = json.dumps({"policy_id": "p-1", "question": "무엇을 준비하나요?", "history": []}, ensure_ascii=False).encode()
        status, _ = self.request("/api/chat", payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.service.chat_calls, [("p-1", "무엇을 준비하나요?", [])])

    def test_factory_errors_are_safe_503_responses(self):
        def failing_factory():
            raise RuntimeError("secret-token=do-not-expose")

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_serverless_handler(failing_factory))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/bootstrap")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=2)
            self.assertEqual(caught.exception.code, 503)
            body = caught.exception.read().decode()
            self.assertIn("service_unavailable", body)
            self.assertNotIn("secret-token", body)
            self.assertNotIn("do-not-expose", body)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_quota_and_provider_errors_have_distinct_safe_http_responses(self):
        class FailingService(FakeService):
            def __init__(self, error):
                super().__init__()
                self.error = error

            def discover(self, query):
                del query
                raise self.error

        def request_with(error):
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_serverless_handler(lambda: FailingService(error)))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/discover",
                    data=json.dumps({"query": "청년 주거"}, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=2)
                return caught.exception.code, json.loads(caught.exception.read())
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

        status, body = request_with(HostedQuotaError())
        self.assertEqual(status, 429)
        self.assertEqual(body, {"error": {"code": "usage_limit", "message": "무료 체험 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."}})

        status, body = request_with(RuntimeError("provider error: secret-token=do-not-expose"))
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "service_unavailable")
        self.assertEqual(body["error"]["message"], "서비스를 준비하지 못했습니다. 잠시 후 다시 시도해 주세요.")
        self.assertNotIn("secret-token", json.dumps(body, ensure_ascii=False))

    def test_non_api_paths_cannot_serve_static_files(self):
        status, body = self.request("/youth_policy/web.py")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        self.assertEqual(self.factory_calls, 0)


if __name__ == "__main__":
    unittest.main()
