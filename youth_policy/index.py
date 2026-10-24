import hashlib
from concurrent.futures import ThreadPoolExecutor
from .domain import metadata_text


def split_text(text: str, size: int, overlap: int) -> list[str]:
    if not 0 <= overlap < size:
        raise ValueError("청크 크기와 중복 길이를 확인하세요.")
    # 자격 조건의 항목 제목과 내용을 함께 보존하는 재귀 분할기.
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap,
                                               separators=["\n\n", "\n", ". ", " ", ""])
    return splitter.split_text(text)


def fingerprint(settings) -> dict:
    if settings.embedding_provider == "local":
        # Chat endpoint changes do not change the local embedding vector space.
        from .local_embeddings import LocalEmbeddings
        base_url = f"local:{LocalEmbeddings.MODEL_ID}@{LocalEmbeddings.REVISION}"
    else:
        base_url = settings.llm_base_url
    return {"embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model, "base_url": base_url,
            "chunk_size": settings.chunk_size, "chunk_overlap": settings.chunk_overlap,
            "chunk_strategy": "policy_sections_v1"}


def split_document(document, size, overlap):
    if (not document.get("sections") or document.get("sections_text_hash") !=
            hashlib.sha256(document["text"].encode()).hexdigest()):
        return split_text(document["text"], size, overlap)
    chunks = []
    for section in document["sections"]:
        prefix = f"정책: {document.get('policy_name', document['title'])[:min(80, size // 4)]}\n문서 구분: {section['label']}\n"
        prefix = prefix[:size // 2]
        budget = size - len(prefix)
        chunks.extend(prefix + part for part in split_text(section["text"], budget, min(overlap, budget - 1)))
    return chunks


class VectorIndex:
    def __init__(self, settings, embedder):
        import chromadb
        from .storage import read_json, write_json
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        manifest = settings.data_dir / "index_settings.json"
        previous = read_json(manifest)
        if previous and previous != fingerprint(settings):
            raise ValueError("색인 설정이 변경되었습니다. DATA_DIR을 새 폴더로 바꾸고 재수집·재색인하세요.")
        self.client = chromadb.PersistentClient(path=str(settings.data_dir / "chroma"))
        self.meta = self.client.get_or_create_collection("policy_meta", embedding_function=None)
        self.docs = self.client.get_or_create_collection("policy_docs", embedding_function=None)
        self.settings, self.embedder = settings, embedder
        write_json(manifest, fingerprint(settings))

    def _upsert(self, collection, ids, texts, metadatas):
        if len(ids) > 512:
            for start in range(0, len(ids), 512):
                self._upsert(collection, ids[start:start+512], texts[start:start+512], metadatas[start:start+512])
            return
        # 같은 내용은 재임베딩하지 않는다. 외부 호출 실패 시 기존 색인을 보존한다.
        hashes = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        old = collection.get(ids=ids, include=["metadatas"])
        existing = dict(zip(old["ids"], old["metadatas"]))
        changed = [i for i, key in enumerate(ids) if existing.get(key, {}).get("content_hash") != hashes[i]]
        metadata_only = [i for i, key in enumerate(ids)
                         if existing.get(key, {}).get("content_hash") == hashes[i]
                         and existing[key] != {**metadatas[i], "content_hash": hashes[i]}]
        if metadata_only:
            collection.update(ids=[ids[i] for i in metadata_only],
                              metadatas=[{**metadatas[i], "content_hash": hashes[i]} for i in metadata_only])
        batches = [changed[start:start + 64] for start in range(0, len(changed), 64)]
        if not batches:
            return

        def encode(batch):
            return self.embedder.embed_documents([texts[i] for i in batch])

        def commit(batch, values):
            collection.upsert(ids=[ids[i] for i in batch], documents=[texts[i] for i in batch],
                              embeddings=values,
                              metadatas=[{**metadatas[i], "content_hash": hashes[i]} for i in batch])

        if len(batches) == 1:
            commit(batches[0], encode(batches[0]))
        else:
            # 외부 임베딩만 최대 3개 요청으로 제한해 병행한다. DB 쓰기는 단일 흐름을 유지한다.
            with ThreadPoolExecutor(max_workers=min(3, len(batches))) as executor:
                futures = [executor.submit(encode, batch) for batch in batches]
                for batch, future in zip(batches, futures):
                    commit(batch, future.result())

    def index_policies(self, policies, progress=None):
        if not policies:
            return
        for start in range(0, len(policies), 128):
            batch = policies[start:start + 128]
            self._upsert(self.meta, [p["policy_id"] for p in batch], [metadata_text(p) for p in batch],
                     [{"policy_id": p["policy_id"], "title": p["name"], "url": p["source_url"],
                       "source_type": "항목 메타데이터",
                       "collected_at": p["collected_at"]} for p in batch])
            if progress:
                progress(min(start + 128, len(policies)), len(policies))

    def index_document(self, policy_id, document, query=""):
        self.index_documents([(policy_id, document)], query=query)

    def index_documents(self, documents, query="", progress=None):
        batch, done = [], 0
        for policy_id, document in documents:
            batch.append(self._prepare_document(policy_id, document, query))
            if len(batch) >= 32:
                self._write_documents(batch)
                done += len(batch)
                batch = []
                if progress:
                    progress(done)
        if batch:
            self._write_documents(batch)
            done += len(batch)
            if progress:
                progress(done)

    def _prepare_document(self, policy_id, document, query):
        texts = split_document(document, self.settings.chunk_size, self.settings.chunk_overlap)
        if not texts:
            raise ValueError("근거 문서 본문이 비어 있습니다.")
        doc_id = hashlib.sha256(document["document_id"].encode()).hexdigest()[:20]
        ids = [f"{policy_id}:{doc_id}:{i}" for i in range(len(texts))]
        metadata = {k: document[k] for k in ["title", "url", "source_type", "collected_at",
                                             "terms_reference", "usage_conditions"]}
        metadata.update(policy_id=policy_id, document_id=document["document_id"], search_query=query)
        return {"ids": ids, "texts": texts, "metadata": [{**metadata, "chunk_index": i} for i in range(len(texts))],
                "policy_id": policy_id, "document_id": document["document_id"]}

    def _write_documents(self, batch):
        self._upsert(self.docs, [key for doc in batch for key in doc["ids"]],
                     [text for doc in batch for text in doc["texts"]],
                     [metadata for doc in batch for metadata in doc["metadata"]])
        # 본문이 짧아져도 이전 버전의 남은 청크가 근거에 섞이지 않게 정리한다.
        for document in batch:
            old = self.docs.get(where={"$and": [{"policy_id": document["policy_id"]},
                                              {"document_id": document["document_id"]}]})
            expected = set(document["ids"])
            stale = [key for key in old["ids"] if key not in expected]
            if stale:
                self.docs.delete(ids=stale)

    @staticmethod
    def records(result, nested=False):
        keys, texts, metas = result["ids"], result["documents"], result["metadatas"]
        if nested:
            keys, texts, metas = keys[0], texts[0], metas[0]
        return [{"id": key, "text": text, **meta} for key, text, meta in zip(keys, texts, metas)]

    def retrieve(self, policy_id, question):
        base = self.records(self.meta.get(ids=[policy_id], include=["documents", "metadatas"]))
        if not base:
            raise ValueError("선택한 정책의 메타데이터 색인이 없습니다. 먼저 색인 명령을 실행하세요.")
        if not self.docs.count():
            return base
        result = self.docs.query(query_embeddings=[self.embedder.embed_query(question)],
                                 where={"policy_id": policy_id}, n_results=self.settings.top_k - 1,
                                 include=["documents", "metadatas"])
        seen = set()
        combined = []
        for source in base + self.records(result, nested=True):
            key = (source["url"], source["text"])
            if key not in seen:
                seen.add(key)
                combined.append(source)
        return combined

    def candidates(self, question, k=20):
        return self.candidates_many([question], k=k)[0]

    def candidates_many(self, questions, k=20):
        """검색어 여러 개를 한 번에 임베딩하고 검색 순서대로 결과를 반환한다."""
        if not questions:
            return []
        count = self.meta.count()
        if not count:
            return [[] for _ in questions]
        encode_queries = getattr(self.embedder, "embed_queries", self.embedder.embed_documents)
        result = self.meta.query(query_embeddings=encode_queries(questions),
                                 n_results=min(k, count), include=["documents", "metadatas"])
        return [self.records({field: result[field][i] for field in ("ids", "documents", "metadatas")})
                for i in range(len(questions))]
