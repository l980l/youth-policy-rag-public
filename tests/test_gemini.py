"""Exercise the actual LangChain/SDK wire format without a paid API call."""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from youth_policy.config import Settings
from youth_policy.llm import embeddings


class GeminiEmbeddingTests(unittest.TestCase):
    def test_documents_and_queries_are_sent_as_strings_not_openai_tokens(self):
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            texts = payload["input"]
            self.assertIsInstance(texts, list)
            self.assertTrue(all(isinstance(text, str) for text in texts))
            self.assertEqual(payload["encoding_format"], "float")
            return httpx.Response(200, json={
                "object": "list", "model": "gemini-embedding-001",
                "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                         for i in range(len(texts))],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })

        with patch.dict(os.environ, {"LLM_API_KEY": "TEST_ONLY"}):
            embedder = embeddings(Settings(embedding_provider="api", embedding_model="gemini-embedding-001"))
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            sdk = OpenAI(api_key="TEST_ONLY", base_url=Settings.llm_base_url, http_client=client)
            embedder.client = sdk.embeddings
            texts = ["청년 취업 교육", "청년 주거 지원"]
            self.assertEqual(len(embedder.embed_documents(texts)), 2)
            self.assertEqual(len(embedder.embed_query("자격증 응시료")), 3)
        self.assertEqual(requests[0]["input"], texts)
        self.assertEqual(requests[1]["input"], ["자격증 응시료"])

    def test_local_fingerprint_ignores_chat_endpoint_but_separates_api_vectors(self):
        from dataclasses import replace
        from youth_policy.index import fingerprint
        settings = Settings()
        self.assertEqual(fingerprint(settings), fingerprint(replace(settings, llm_base_url="https://other.invalid/v1/")))
        self.assertNotEqual(fingerprint(settings), fingerprint(replace(settings, embedding_provider="api")))


if __name__ == "__main__":
    unittest.main()
