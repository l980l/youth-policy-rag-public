import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    youth_api_url: str = "https://www.youthcenter.go.kr/go/ythip/getPlcy"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    top_k: int = 6
    request_interval: float = 0.5
    llm_model: str = "gemini-3.5-flash-lite"
    embedding_provider: str = "local"
    embedding_model: str = "intfloat/multilingual-e5-small"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __post_init__(self):
        if self.embedding_provider not in {"local", "api"}:
            raise ValueError("EMBEDDING_PROVIDER는 local 또는 api여야 합니다.")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("CHUNK_OVERLAP은 0 이상 CHUNK_SIZE 미만이어야 합니다.")
        if self.top_k < 2 or self.request_interval < 0:
            raise ValueError("TOP_K는 2 이상, REQUEST_INTERVAL은 0 이상이어야 합니다.")


def load_settings() -> Settings:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    path = Path(os.getenv("DATA_DIR", "data"))
    return Settings(
        data_dir=path if path.is_absolute() else ROOT / path,
        youth_api_url=os.getenv("YOUTH_API_URL", Settings.youth_api_url),
        chunk_size=int(os.getenv("CHUNK_SIZE", "1000")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "200")),
        top_k=int(os.getenv("TOP_K", "6")),
        request_interval=float(os.getenv("REQUEST_INTERVAL", "0.5")),
        llm_model=os.getenv("LLM_MODEL", Settings.llm_model),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", Settings.embedding_provider),
        embedding_model=os.getenv("EMBEDDING_MODEL", Settings.embedding_model),
        llm_base_url=os.getenv("LLM_BASE_URL", Settings.llm_base_url),
    )


def llm_key() -> str:
    key = os.getenv("LLM_API_KEY") or os.getenv("GMS_KEY")
    if not key:
        raise ValueError(".env에 GMS_KEY 또는 LLM_API_KEY를 설정하세요.")
    return key
