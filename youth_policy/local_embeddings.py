"""Pinned, local multilingual E5 embeddings for policy retrieval."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from uuid import uuid4

import numpy as np


class LocalEmbeddings:
    """A small LangChain-compatible embedding adapter backed by ONNX Runtime.

    The files are fetched only when this adapter is first used.  They are pinned
    to an immutable Hugging Face revision so a later upstream model update cannot
    silently alter an existing Chroma index.
    """

    MODEL_ID = "intfloat/multilingual-e5-small"
    REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    MODEL_FILE = "onnx/model.onnx"
    TOKENIZER_FILE = "onnx/tokenizer.json"
    DIMENSIONS = 384
    MAX_TOKENS = 512
    BATCH_SIZE = 16

    def __init__(self, cache_dir: Path, *, session=None, tokenizer=None):
        self.cache_dir = Path(cache_dir)
        self._session = session
        self._tokenizer = tokenizer
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()

    @property
    def identifier(self) -> str:
        return f"{self.MODEL_ID}@{self.REVISION}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"passage: {self._text(text)}" for text in texts])

    def embed_query(self, text: str) -> list[float]:
        values = self.embed_queries([text])
        return values[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self._embed([f"query: {self._text(text)}" for text in texts])

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str):
            raise TypeError("임베딩 입력은 문자열이어야 합니다.")
        return value

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        # Similar lengths reduce CPU work spent on padding. Restore caller order.
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        vectors = [None] * len(texts)
        for start in range(0, len(order), self.BATCH_SIZE):
            positions = order[start:start + self.BATCH_SIZE]
            batch = self._run_batch([texts[index] for index in positions])
            for index, vector in zip(positions, batch):
                vectors[index] = vector.tolist()
        return vectors

    def _load(self):
        if self._session is not None and self._tokenizer is not None:
            return
        with self._load_lock:
            if self._session is not None and self._tokenizer is not None:
                return
            model_path = self._asset(self.MODEL_FILE)
            tokenizer_path = self._asset(self.TOKENIZER_FILE)
            import onnxruntime as ort
            from tokenizers import Tokenizer
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(str(model_path), sess_options=options,
                                                  providers=["CPUExecutionProvider"])
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._tokenizer.enable_truncation(max_length=self.MAX_TOKENS)
            self._tokenizer.no_padding()

    def _asset(self, relative_path: str) -> Path:
        destination = self.cache_dir / relative_path
        if destination.is_file() and destination.stat().st_size:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        import requests
        url = f"https://huggingface.co/{self.MODEL_ID}/resolve/{self.REVISION}/{relative_path}"
        try:
            with requests.get(url, stream=True, timeout=(10, 120)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            if not temporary.stat().st_size:
                raise ValueError("다운로드한 로컬 임베딩 모델 파일이 비어 있습니다.")
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def _run_batch(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        width = min(self.MAX_TOKENS, max(len(item.ids) for item in encodings))
        input_ids = np.full((len(encodings), width), 1, dtype=np.int64)
        attention_mask = np.zeros((len(encodings), width), dtype=np.int64)
        token_type_ids = np.zeros((len(encodings), width), dtype=np.int64)
        for row, item in enumerate(encodings):
            length = min(width, len(item.ids))
            input_ids[row, :length] = item.ids[:length]
            attention_mask[row, :length] = item.attention_mask[:length]
            token_type_ids[row, :length] = item.type_ids[:length]
        available = {item.name for item in self._session.get_inputs()}
        feeds = {}
        if "input_ids" in available:
            feeds["input_ids"] = input_ids
        if "attention_mask" in available:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in available:
            feeds["token_type_ids"] = token_type_ids
        if not feeds:
            raise ValueError("로컬 임베딩 ONNX 모델의 입력 형식을 확인할 수 없습니다.")
        with self._run_lock:
            hidden = np.asarray(self._session.run(None, feeds)[0], dtype=np.float32)
        mask = attention_mask[..., None].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-12, None)
        return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
