import unittest
from pathlib import Path

import numpy as np

from youth_policy.local_embeddings import LocalEmbeddings


class Encoding:
    def __init__(self, ids, attention_mask=None, type_ids=None):
        self.ids = ids
        self.attention_mask = attention_mask or [1] * len(ids)
        self.type_ids = type_ids or [0] * len(ids)


class Tokenizer:
    def __init__(self):
        self.calls = []

    def encode_batch(self, texts):
        self.calls.extend(texts)
        return [Encoding([index + 1, index + 11, 0], [1, 1, 0])
                for index, _ in enumerate(texts)]


class Input:
    def __init__(self, name):
        self.name = name


class Session:
    def __init__(self):
        self.feeds = []

    def get_inputs(self):
        return [Input("input_ids"), Input("attention_mask"), Input("token_type_ids")]

    def run(self, _, feeds):
        self.feeds.append(feeds)
        # The last token is a deliberately large padding vector.  Correct
        # attention-mask mean pooling must exclude it.
        hidden = np.array([[[3., 0.], [1., 2.], [99., 99.]] for _ in feeds["input_ids"]], dtype=np.float32)
        return [hidden]


class LocalEmbeddingsTests(unittest.TestCase):
    def setUp(self):
        self.tokenizer = Tokenizer()
        self.session = Session()
        self.embedder = LocalEmbeddings(Path("unused"), session=self.session, tokenizer=self.tokenizer)

    def test_prefixes_inputs_and_preserves_batched_document_order(self):
        self.embedder.BATCH_SIZE = 2
        values = self.embedder.embed_documents(["첫째", "둘째", "셋째"])
        self.assertEqual(self.tokenizer.calls, ["passage: 첫째", "passage: 둘째", "passage: 셋째"])
        self.assertEqual(len(values), 3)
        self.assertEqual(len(self.session.feeds), 2)

    def test_query_uses_query_prefix_and_returns_one_vector(self):
        vector = self.embedder.embed_query("지원 조건")
        self.assertEqual(self.tokenizer.calls, ["query: 지원 조건"])
        self.assertEqual(len(vector), 2)

    def test_search_query_batch_uses_query_prefix_not_document_prefix(self):
        from unittest.mock import Mock
        from youth_policy.index import VectorIndex
        index = VectorIndex.__new__(VectorIndex)
        index.embedder = self.embedder
        index.meta = Mock()
        index.meta.count.return_value = 2
        index.meta.query.return_value = {"ids": [[], []], "documents": [[], []], "metadatas": [[], []]}
        self.assertEqual(index.candidates_many(["취업", "주거"]), [[], []])
        self.assertEqual(self.tokenizer.calls, ["query: 취업", "query: 주거"])

    def test_masked_mean_pooling_excludes_padding_and_normalizes(self):
        vector = np.array(self.embedder.embed_query("질문"))
        expected = np.array([2., 1.]) / np.sqrt(5.)
        np.testing.assert_allclose(vector, expected, atol=1e-6)
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=6)

    def test_empty_documents_need_no_tokenizer_or_model_call(self):
        self.assertEqual(self.embedder.embed_documents([]), [])
        self.assertEqual(self.tokenizer.calls, [])
        self.assertEqual(self.session.feeds, [])

    def test_different_length_documents_keep_their_original_vector_order(self):
        class OrderedTokenizer:
            def encode_batch(self, texts):
                return [Encoding([int(text.rsplit("-", 1)[1])]) for text in texts]

        class OrderedSession(Session):
            def run(self, _, feeds):
                ids = feeds["input_ids"].astype(np.float32)
                return [np.stack([ids, np.ones_like(ids)], axis=-1)]

        embedder = LocalEmbeddings(Path("unused"), session=OrderedSession(), tokenizer=OrderedTokenizer())
        embedder.BATCH_SIZE = 2
        values = embedder.embed_documents(["매우 긴 정책 문장-1", "짧음-2", "중간 문장-3"])
        for identifier, vector in enumerate(values, start=1):
            expected = np.array([identifier, 1.]) / np.linalg.norm([identifier, 1.])
            np.testing.assert_allclose(vector, expected, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
