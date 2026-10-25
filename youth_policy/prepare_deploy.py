"""Build a clean deployment seed. Generated policy/model assets never enter Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

from .config import ROOT, load_settings
from .domain import FIELDS, policy_document
from .index import fingerprint
from .local_embeddings import LocalEmbeddings
from .storage import load_policies, write_json


META_FIELDS = {'policy_id', 'title', 'url', 'source_type', 'collected_at', 'content_hash'}
DOC_FIELDS = META_FIELDS | {'document_id', 'chunk_index', 'terms_reference', 'usage_conditions'}
RAW_FIELDS = set(FIELDS) | {'plcyNo', 'plcyNm', 'lclsfNm', 'mclsfNm', 'plcyKywdNm'}


def _selected_records(batch, name, ids, allowed):
    selected = [i for i, meta in enumerate(batch['metadatas'])
                if meta.get('policy_id') in ids and (name != 'policy_docs' or
                   meta.get('document_id') == meta.get('policy_id'))]
    metas = [{k: v for k, v in batch['metadatas'][i].items() if k in allowed} for i in selected]
    if name == 'policy_docs':
        for meta in metas:
            # The deployment has no saved user-search context.
            meta['search_query'] = ''
    return selected, metas


def _read_embeddings(collection, ids):
    """Return readable stored vectors, isolating only corrupt records by ID."""
    values = {}

    def read(keys):
        try:
            result = collection.get(ids=keys, include=['embeddings'])
            values.update(zip(result['ids'], result['embeddings']))
        except Exception:
            if len(keys) > 1:
                midpoint = len(keys) // 2
                read(keys[:midpoint])
                read(keys[midpoint:])

    read(ids)
    return values


def export_vectors(source_data, destination, policies, embedder=None):
    """Copy vectors into a NEW DB, with no source query metadata or related documents."""
    import chromadb
    src = chromadb.PersistentClient(path=str(source_data / 'chroma'))
    dst = chromadb.PersistentClient(path=str(destination / 'chroma'))
    ids = {p['policy_id'] for p in policies}
    counts = {}
    try:
        for name, allowed in [('policy_meta', META_FIELDS), ('policy_docs', DOC_FIELDS)]:
            source = src.get_collection(name, embedding_function=None)
            target = dst.get_or_create_collection(name, embedding_function=None)
            copied = 0
            covered = set()
            for offset in range(0, source.count(), 256):
                if name == 'policy_docs':
                    # Filter before reading vectors: older caches can contain a
                    # corrupt related/search record which is not deployable anyway.
                    batch = source.get(limit=256, offset=offset, include=['documents', 'metadatas'])
                    embeddings = None
                else:
                    batch = source.get(limit=256, offset=offset,
                                       include=['embeddings', 'documents', 'metadatas'])
                    embeddings = batch['embeddings']
                selected, metas = _selected_records(batch, name, ids, allowed)
                if not selected:
                    continue
                documents = [batch['documents'][i] for i in selected]
                if embeddings is not None:
                    values = [embeddings[i].tolist() for i in selected]
                else:
                    if embedder is None:
                        raise ValueError('복구용 로컬 임베딩 모델이 필요합니다.')
                    selected_ids = [batch['ids'][index] for index in selected]
                    stored = _read_embeddings(source, selected_ids)
                    missing = [index for index, key in enumerate(selected_ids) if key not in stored]
                    rebuilt = iter(embedder.embed_documents([documents[i] for i in missing]))
                    values = [(stored[selected_ids[position]].tolist()
                               if selected_ids[position] in stored else next(rebuilt))
                              for position in range(len(selected))]
                target.add(ids=[batch['ids'][i] for i in selected],
                           embeddings=values, documents=documents, metadatas=metas)
                covered.update(m['policy_id'] for m in metas)
                copied += len(selected)
            if covered != ids:
                raise ValueError('배포 색인에서 정책이 누락됐습니다.')
            counts[name] = copied
            print(f'{name}: {copied}', flush=True)
    finally:
        src.close()
        dst.close()
    return counts


def prepare(source_data=None, model_cache=None):
    assets = ROOT / 'runtime_assets'
    if assets.exists():
        raise ValueError('runtime_assets가 이미 있습니다. 기존 생성 결과를 확인하세요.')
    seed = assets / 'seed'
    seed.mkdir(parents=True)
    settings = replace(load_settings(), data_dir=seed)
    if settings.embedding_provider != 'local':
        raise ValueError('무료 배포는 로컬 임베딩만 지원합니다.')
    target_model = assets / 'models' / LocalEmbeddings.REVISION
    if model_cache:
        model_cache = Path(model_cache)
        for name in (LocalEmbeddings.MODEL_FILE, LocalEmbeddings.TOKENIZER_FILE):
            dest = target_model / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model_cache / name, dest)
    else:
        model = LocalEmbeddings(target_model)
        model._asset(LocalEmbeddings.MODEL_FILE)
        model._asset(LocalEmbeddings.TOKENIZER_FILE)
    if source_data is not None:
        source_data = Path(source_data)
        if json.loads((source_data / 'index_settings.json').read_text()) != fingerprint(settings):
            raise ValueError('원본 색인 설정이 배포 설정과 다릅니다.')
        policies = load_policies(source_data)
        if not policies:
            raise ValueError('배포할 정책 데이터가 없습니다.')
        # No raw source caches, questions, sessions, traces or unknown fields are copied.
        for p in policies:
            p['raw'] = {k: v for k, v in p['raw'].items() if k in RAW_FIELDS}
        write_json(seed / 'policies.json', policies)
        write_json(seed / 'index_settings.json', fingerprint(settings))
        counts = export_vectors(source_data, seed, policies, LocalEmbeddings(target_model))
    else:
        from .collector import YouthClient
        from .index import VectorIndex
        client = YouthClient(settings)
        policies = client.collect_all()
        if not policies:
            raise ValueError('정책 수집 결과가 비어 있습니다.')
        for p in policies:
            p['raw'] = {k: v for k, v in p['raw'].items() if k in RAW_FIELDS}
        write_json(seed / 'policies.json', policies)
        index = VectorIndex(settings, LocalEmbeddings(target_model))
        try:
            index.index_policies(policies, progress=lambda a, b: print(f'metadata: {a}/{b}', flush=True))
            index.index_documents(((p['policy_id'], policy_document(p)) for p in policies),
                                  progress=lambda n: print(f'documents: {n}/{len(policies)}', flush=True))
            counts = {'policy_meta': index.meta.count(), 'policy_docs': index.docs.count()}
        finally:
            index.client.close()
    for p in policies:
        name = hashlib.sha256(p['policy_id'].encode()).hexdigest() + '.json'
        write_json(seed / 'documents' / name, policy_document(p))
    size = sum(p.stat().st_size for p in seed.rglob('*') if p.is_file())
    if size > 256 * 1024 * 1024:
        raise ValueError('배포 데이터가 무료 체험용 임시 저장 한도를 초과합니다.')
    write_json(assets / 'manifest.json', {'schema': 1, 'policy_count': len(policies),
               'counts': counts, 'index_settings': fingerprint(settings), 'seed_bytes': size,
               'source_collected_at': max(p.get('collected_at', '') for p in policies)})
    print(json.dumps({'policy_count': len(policies), 'counts': counts, 'seed_bytes': size}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-data', type=Path)
    parser.add_argument('--model-cache', type=Path)
    args = parser.parse_args()
    try:
        prepare(args.source_data, args.model_cache)
    except Exception as exc:
        # Never include raw provider exception strings, which may contain a key URL.
        raise SystemExit(f'배포 자료 준비 실패({type(exc).__name__}). 설정·네트워크·생성 폴더를 확인하세요.') from None


if __name__ == '__main__':
    main()
