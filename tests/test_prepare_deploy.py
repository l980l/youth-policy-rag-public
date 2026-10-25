import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from youth_policy.prepare_deploy import export_vectors


class FakeCollection:
    def __init__(self, records, *, broken_embeddings=()):
        self.records = records
        self.broken_embeddings = set(broken_embeddings)
        self.added = []

    def count(self):
        return len(self.records)

    def get(self, limit=None, offset=0, include=(), ids=None):
        records = (self.records if ids is None else [record for record in self.records if record['id'] in ids])
        if 'embeddings' in include and any(record['id'] in self.broken_embeddings for record in records):
            raise RuntimeError('stored vector segment is unreadable')
        records = records[offset:] if limit is None else records[offset:offset + limit]
        result = {
            'ids': [record['id'] for record in records],
            'documents': [record['document'] for record in records],
            'metadatas': [record['metadata'] for record in records],
        }
        if 'embeddings' in include:
            result['embeddings'] = [record['embedding'] for record in records]
        return result

    def add(self, **values):
        self.added.append(values)


class FakeClient:
    def __init__(self, collections):
        self.collections = collections
        self.closed = False

    def get_collection(self, name, embedding_function=None):
        return self.collections[name]

    def get_or_create_collection(self, name, embedding_function=None):
        return self.collections.setdefault(name, FakeCollection([]))

    def close(self):
        self.closed = True


class FakeEmbeddings:
    def __init__(self):
        self.documents = []

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [[float(index)] for index, _ in enumerate(texts)]


class ExportVectorsTests(unittest.TestCase):
    def test_reembeds_only_public_own_documents_when_stored_vectors_fail(self):
        source_collections = {
            'policy_meta': FakeCollection([
                {'id': 'meta-a', 'document': 'A', 'embedding': np.array([1.0]),
                 'metadata': {'policy_id': 'a', 'title': 'A', 'url': 'https://a',
                              'source_type': 'meta', 'collected_at': '2026-01-01',
                              'private': 'drop'}},
                {'id': 'meta-b', 'document': 'B', 'embedding': np.array([2.0]),
                 'metadata': {'policy_id': 'b', 'title': 'B', 'url': 'https://b',
                              'source_type': 'meta', 'collected_at': '2026-01-01'}},
            ]),
            'policy_docs': FakeCollection([
                {'id': 'doc-a', 'document': 'own policy A', 'embedding': np.array([3.0]),
                 'metadata': {'policy_id': 'a', 'document_id': 'a', 'chunk_index': 0,
                              'title': 'A', 'url': 'https://a', 'source_type': 'detail',
                              'collected_at': '2026-01-01', 'terms_reference': 'terms',
                              'usage_conditions': 'conditions', 'search_query': 'private question'}},
                {'id': 'doc-a-related', 'document': 'related result', 'embedding': np.array([4.0]),
                 'metadata': {'policy_id': 'a', 'document_id': 'other', 'chunk_index': 0,
                              'title': 'other', 'url': 'https://other', 'source_type': 'search',
                              'collected_at': '2026-01-01', 'terms_reference': 'terms',
                              'usage_conditions': 'conditions', 'search_query': 'private question'}},
                {'id': 'doc-b', 'document': 'own policy B', 'embedding': np.array([5.0]),
                 'metadata': {'policy_id': 'b', 'document_id': 'b', 'chunk_index': 0,
                              'title': 'B', 'url': 'https://b', 'source_type': 'detail',
                              'collected_at': '2026-01-01', 'terms_reference': 'terms',
                              'usage_conditions': 'conditions', 'search_query': 'private question'}},
            ], broken_embeddings={'doc-b'}),
        }
        destination_collections = {}
        source = FakeClient(source_collections)
        destination = FakeClient(destination_collections)
        clients = [source, destination]
        chromadb = SimpleNamespace(PersistentClient=lambda path: clients.pop(0))
        embedder = FakeEmbeddings()

        with patch.dict(sys.modules, {'chromadb': chromadb}):
            counts = export_vectors(Path('/source'), Path('/destination'),
                                    [{'policy_id': 'a'}, {'policy_id': 'b'}], embedder)

        self.assertEqual(counts, {'policy_meta': 2, 'policy_docs': 2})
        self.assertEqual(embedder.documents, ['own policy B'])
        doc_values = destination_collections['policy_docs'].added[0]
        self.assertEqual(doc_values['ids'], ['doc-a', 'doc-b'])
        self.assertEqual([item['search_query'] for item in doc_values['metadatas']], ['', ''])
        self.assertNotIn('private', destination_collections['policy_meta'].added[0]['metadatas'][0])
        self.assertTrue(source.closed)
        self.assertTrue(destination.closed)


if __name__ == '__main__':
    unittest.main()
