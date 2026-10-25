import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from .config import llm_key
from .usage import HostedConfigError, HostedUsageQuota, is_hosted, validate_free_config


class Model:
    def __init__(self, settings):
        self.hosted = is_hosted()
        self.quota = None
        if self.hosted:
            validate_free_config(settings)
            api_key = os.getenv("LLM_API_KEY", "").strip()
            if not api_key:
                raise HostedConfigError("호스팅 AI 기능 설정을 확인해 주세요.")
            self.quota = HostedUsageQuota(Path(settings.data_dir) / "hosted_usage.sqlite3")
            options = {"timeout": 45, "max_retries": 0, "max_tokens": 4096}
        else:
            api_key = llm_key()
            options = {"timeout": 60, "max_retries": 2}
        from langchain_openai import ChatOpenAI
        self.chat = ChatOpenAI(model=settings.llm_model, api_key=api_key,
                               base_url=settings.llm_base_url, **options)

    def text(self, system: str, payload) -> str:
        serialized = json.dumps(payload, ensure_ascii=False)
        if self.hosted and len(serialized) > 120_000:
            raise ValueError("요청 내용이 너무 큽니다.")
        if self.quota is not None:
            self.quota.consume()
        content = self.chat.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": serialized},
        ]).content
        if not isinstance(content, str):
            raise ValueError("모델이 텍스트 응답을 반환하지 않았습니다.")
        return content

    def json(self, system: str, payload) -> dict:
        response = self.text(system, payload).strip()
        if response.startswith("```"):
            response = response.split("\n", 1)[1].rsplit("```", 1)[0]
        value = json.loads(response)
        if not isinstance(value, dict):
            raise ValueError("모델 JSON 형식이 올바르지 않습니다.")
        return value


def embeddings(settings):
    if settings.embedding_provider == "local":
        from .config import ROOT
        from .local_embeddings import LocalEmbeddings
        if settings.embedding_model != "intfloat/multilingual-e5-small":
            raise ValueError("로컬 임베딩 모델은 intfloat/multilingual-e5-small을 사용하세요.")
        cache = os.getenv("LOCAL_EMBEDDING_CACHE", "").strip() if is_hosted() else ""
        if cache:
            cache_path = Path(cache)
            model_path = cache_path if cache_path.name == LocalEmbeddings.REVISION else cache_path / LocalEmbeddings.REVISION
            return LocalEmbeddings(model_path)
        return LocalEmbeddings(ROOT / "data" / "models" / "multilingual-e5-small" / LocalEmbeddings.REVISION)
    if is_hosted():
        validate_free_config(settings)
    from langchain_openai import OpenAIEmbeddings
    options = {}
    if urlsplit(settings.llm_base_url).hostname == "generativelanguage.googleapis.com":
        # Gemini's compatible endpoint accepts text, not OpenAI token ID arrays.
        options = {"check_embedding_ctx_length": False,
                   "model_kwargs": {"encoding_format": "float"}}
    return OpenAIEmbeddings(model=settings.embedding_model, api_key=llm_key(),
                            base_url=settings.llm_base_url, request_timeout=60, max_retries=2,
                            **options)
