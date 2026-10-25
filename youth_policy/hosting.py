"""Read-only deployment seed -> writable ephemeral runtime; never load private caches."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from .config import ROOT, load_settings
from .index import fingerprint
from .local_embeddings import LocalEmbeddings
from .web import PolicyService

_lock = threading.Lock()
_service = None
MAX_SEED_BYTES = 256 * 1024 * 1024


def prepare_runtime(assets: Path, settings, temporary_root: Path | None = None):
    assets = Path(assets)
    manifest = json.loads((assets / 'manifest.json').read_text())
    seed = assets / 'seed'
    if manifest.get('index_settings') != fingerprint(settings):
        raise ValueError('배포 검색 설정과 색인이 다릅니다.')
    for name in ('policies.json', 'index_settings.json', 'chroma/chroma.sqlite3'):
        if not (seed / name).is_file():
            raise ValueError('배포 데이터가 준비되지 않았습니다.')
    total = sum(p.stat().st_size for p in seed.rglob('*') if p.is_file())
    if total > MAX_SEED_BYTES:
        raise ValueError('무료 체험용 임시 저장 용량을 초과합니다.')
    model_cache = assets / 'models' / LocalEmbeddings.REVISION
    for name in (LocalEmbeddings.MODEL_FILE, LocalEmbeddings.TOKENIZER_FILE):
        if not (model_cache / name).is_file():
            raise ValueError('배포 검색 모델이 준비되지 않았습니다.')
    # Include actual SQLite content, not a user-controlled directory in the manifest.
    digest = hashlib.sha256((seed / 'chroma/chroma.sqlite3').read_bytes()).hexdigest()[:20]
    base = Path(temporary_root or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    destination = base / ('youth-policy-' + digest)
    # Vercel runs Linux. The file lock also covers concurrent processes sharing /tmp.
    import fcntl
    with (base / ('youth-policy-' + digest + '.lock')).open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if not (destination / '.ready').is_file():
            partial = Path(tempfile.mkdtemp(prefix='youth-seed-', dir=base))
            try:
                shutil.copytree(seed, partial, dirs_exist_ok=True)
                (partial / '.ready').write_text('ready')
                if destination.exists():
                    shutil.rmtree(destination)
                partial.rename(destination)
            finally:
                if partial.exists():
                    shutil.rmtree(partial)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return replace(settings, data_dir=destination), model_cache


class HostedPolicyService(PolicyService):
    """At most one AI operation per live runtime; reject rather than queue work."""

    def __init__(self, settings):
        super().__init__(settings)
        self._demo_gate = threading.BoundedSemaphore(1)

    def bootstrap(self):
        from .usage import public_status
        data = super().bootstrap()
        data['demo'] = public_status(self.settings)
        return data

    def _limited(self, action):
        from .usage import HostedQuotaError
        if not self._demo_gate.acquire(blocking=False):
            raise HostedQuotaError()
        try:
            return action()
        finally:
            self._demo_gate.release()

    def chat(self, policy_id, question, history):
        return self._limited(lambda: super(HostedPolicyService, self).chat(policy_id, question, history))

    def discover(self, query):
        return self._limited(lambda: super(HostedPolicyService, self).discover(query))


def get_service():
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                settings, model_cache = prepare_runtime(ROOT / 'runtime_assets', load_settings())
                os.environ['LOCAL_EMBEDDING_CACHE'] = str(model_cache)
                _service = HostedPolicyService(settings)
    return _service
