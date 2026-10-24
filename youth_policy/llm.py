import json
from urllib.parse import urlsplit
from .config import llm_key


class Model:
    def __init__(self, settings):
        from langchain_openai import ChatOpenAI
        self.chat = ChatOpenAI(model=settings.llm_model, api_key=llm_key(),
                               base_url=settings.llm_base_url, timeout=60, max_retries=2)

    def text(self, system: str, payload) -> str:
        content = self.chat.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
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
        return LocalEmbeddings(ROOT / "data" / "models" / "multilingual-e5-small" / LocalEmbeddings.REVISION)
    from langchain_openai import OpenAIEmbeddings
    options = {}
    if urlsplit(settings.llm_base_url).hostname == "generativelanguage.googleapis.com":
        # Gemini's compatible endpoint accepts text, not OpenAI token ID arrays.
        options = {"check_embedding_ctx_length": False,
                   "model_kwargs": {"encoding_format": "float"}}
    return OpenAIEmbeddings(model=settings.embedding_model, api_key=llm_key(),
                            base_url=settings.llm_base_url, request_timeout=60, max_retries=2,
                            **options)
