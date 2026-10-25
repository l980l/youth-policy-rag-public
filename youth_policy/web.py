"""Local, same-origin JSON API and static server for the policy web client."""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .config import ROOT, load_settings
from .domain import clean, policy_document
from .storage import load_policies
from .usage import HostedQuotaError


MAX_BODY_BYTES = 32 * 1024
MAX_QUERY_LENGTH = 500
MAX_QUESTION_LENGTH = 1_000
MAX_HISTORY_MESSAGES = 6
CATEGORY_ORDER = ("일자리", "주거", "교육", "복지·문화", "참여·권리", "기타")
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".png": "image/png",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}


class RequestError(ValueError):
    """A client-input error that can be rendered without exposing internals."""


class NotFoundError(LookupError):
    """A requested, public resource does not exist."""


def _text(value: object) -> str:
    return " ".join(clean(value).split())


def primary_category(policy: dict) -> str:
    """Map varied source classifications to one stable UI group, in display order."""
    # lclsfNm is the source's primary class.  Looking at mclsfNm first turns
    # "교육·직업훈련" into employment, even where the source classified it as education.
    value = str(policy.get("category") or policy.get("subcategory") or "").casefold()
    checks = (
        ("일자리", ("일자리", "취업", "창업")),
        ("주거", ("주거", "주택", "전월세", "임대")),
        ("교육", ("교육", "훈련", "학습", "인재")),
        ("복지·문화", ("복지", "문화", "금융", "건강", "생활", "보육")),
        ("참여·권리", ("참여", "권리", "활동", "정책", "기반")),
    )
    return next((name for name, words in checks if any(word in value for word in words)), "기타")


def age_label(policy: dict) -> str:
    raw = policy.get("raw", {})
    if not isinstance(raw, dict):
        return ""

    def age(key: str) -> int | None:
        value = _text(raw.get(key))
        matched = re.fullmatch(r"\d{1,3}", value)
        parsed = int(value) if matched else None
        return parsed if parsed is not None and 10 <= parsed <= 100 else None

    minimum, maximum = age("sprtTrgtMinAge"), age("sprtTrgtMaxAge")
    if minimum is not None and maximum is not None:
        return f"만 {minimum}~{maximum}세" if minimum <= maximum else ""
    if minimum is not None:
        return f"만 {minimum}세 이상"
    if maximum is not None:
        return f"만 {maximum}세 이하"
    return ""


def policy_summary(policy: dict) -> dict:
    raw = policy.get("raw", {}) if isinstance(policy.get("raw", {}), dict) else {}
    summary = _text(raw.get("plcyExplnCn")) or _text(raw.get("plcySprtCn"))
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "…"
    return {
        "policy_id": str(policy.get("policy_id", "")),
        "name": _text(policy.get("name")),
        "category": primary_category(policy),
        "subcategory": _text(policy.get("subcategory")),
        "institution": _text(raw.get("sprvsnInstCdNm")) or _text(raw.get("operInstCdNm")),
        "summary": summary,
        "age_label": age_label(policy),
        "source_url": _text(policy.get("source_url")),
        "collected_at": _text(policy.get("collected_at")),
        "evidence_quality": _text(policy.get("evidence_quality")),
    }


class PolicyService:
    """Catalogue reads remain cheap; model/index resources are created only when needed."""

    def __init__(self, settings=None, policies: list[dict] | None = None):
        self.settings = settings or load_settings()
        self.policies = policies if policies is not None else load_policies(self.settings.data_dir)
        self.by_id = {str(policy.get("policy_id")): policy for policy in self.policies}
        self._runtime_lock = threading.RLock()
        self._chat_lock = threading.Lock()
        self._model = self._index = self._collector = self._graph = None

    def _get_index(self):
        if self._index is None:
            with self._runtime_lock:
                if self._index is None:
                    from .index import VectorIndex
                    from .llm import embeddings
                    self._index = VectorIndex(self.settings, embeddings(self.settings))
        return self._index

    def _runtime(self):
        if self._graph is None:
            with self._runtime_lock:
                if self._graph is None:
                    from .collector import YouthClient
                    from .llm import Model
                    from .rag import build_graph
                    from .sources import SourceCollector
                    model = Model(self.settings)
                    index = self._get_index()
                    client = YouthClient(self.settings)
                    self._model, self._index = model, index
                    self._collector = SourceCollector(self.settings, client, model, index)
                    self._graph = build_graph(index, model)
        return self._model, self._index, self._collector, self._graph

    def categories(self) -> list[dict]:
        counts = {name: 0 for name in CATEGORY_ORDER}
        for policy in self.policies:
            counts[primary_category(policy)] += 1
        return [{"name": name, "count": counts[name]} for name in CATEGORY_ORDER]

    def bootstrap(self) -> dict:
        index = self._get_index()
        return {
            "stats": {
                "policy_count": len(self.policies),
                "metadata_count": index.meta.count(),
                "document_chunk_count": index.docs.count(),
                "collected_at": max((_text(p.get("collected_at")) for p in self.policies), default=""),
            },
            "categories": self.categories(),
        }

    def list_policies(self, query: str, category: str, page: int, limit: int) -> dict:
        if category and category != "전체" and category not in CATEGORY_ORDER:
            raise RequestError("알 수 없는 정책 분야입니다.")
        needle = query.casefold()
        filtered = []
        for policy in self.policies:
            summary = policy_summary(policy)
            haystack = " ".join(str(summary[key]) for key in ("name", "category", "subcategory", "institution", "summary")).casefold()
            if (not needle or needle in haystack) and (not category or category == "전체" or summary["category"] == category):
                filtered.append(summary)
        total = len(filtered)
        start = (page - 1) * limit
        return {"items": filtered[start:start + limit], "total": total, "page": page,
                "limit": limit, "total_pages": (total + limit - 1) // limit}

    def detail(self, policy_id: str) -> dict:
        policy = self.by_id.get(policy_id)
        if policy is None:
            raise NotFoundError("정책을 찾을 수 없습니다.")
        document = policy_document(policy)
        return {"policy": policy_summary(policy), "sections": document["sections"]}

    def discover(self, query: str) -> dict:
        from .discover import explore
        model, index, _, _ = self._runtime()
        started = time.monotonic()
        # The model wrapper is not guaranteed thread-safe. Catalogue routes never acquire this lock.
        with self._chat_lock:
            result = explore(query, self.policies, index, model)
        return {**result, "matches": [self._match(item) for item in result["matches"]],
                "needs_confirmation": [self._match(item) for item in result["needs_confirmation"]],
                "seconds": round(time.monotonic() - started, 2)}

    @staticmethod
    def _match(item: dict) -> dict:
        return {**item, "policy": policy_summary(item["policy"])}

    def chat(self, policy_id: str, question: str, history: list[dict]) -> dict:
        from .rag import cited_sources
        policy = self.by_id.get(policy_id)
        if policy is None:
            raise NotFoundError("정책을 찾을 수 없습니다.")
        model, index, collector, graph = self._runtime()
        del model, index
        started = time.monotonic()
        # ensure() writes document/query caches, so protect a process-local cache mutation sequence.
        with self._chat_lock:
            trace = collector.ensure(policy, question, history)
            result = graph.invoke({"policy_id": policy_id, "question": question, "history": history})
            if result.get("sources") and "질문을 뒷받침하는 유효한 근거 인용이 없습니다." in result.get("answer", ""):
                # Retry a format failure once; never invent citations or retry an unknown-date guard.
                result = graph.invoke({"policy_id": policy_id,
                                       "question": question + "\n주요 사실마다 제공된 번호로 [근거 N] 형식의 인용을 반드시 붙여 주세요.",
                                       "history": history})
        sources = []
        for number, source in cited_sources(result.get("answer", ""), result.get("sources", [])):
            sources.append({"number": number, "title": _text(source.get("title")), "url": _safe_url(source.get("url")),
                            "text": clean(source.get("text")), "source_type": _text(source.get("source_type")),
                            "collected_at": _text(source.get("collected_at"))})
        return {"answer": result.get("answer", ""), "sources": sources,
                "seconds": round(time.monotonic() - started, 2),
                "partial_sources": any(item.get("status") == "external_search_unavailable" for item in trace)}


def _safe_url(value: object) -> str:
    value = _text(value)
    return value if urlsplit(value).scheme == "https" else ""


def _integer(value: str | None, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    if not value.isascii() or not value.isdigit():
        raise RequestError(f"{name} 값이 올바르지 않습니다.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise RequestError(f"{name} 값이 올바르지 않습니다.")
    return parsed


def _bounded_text(value: object, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field}은(는) 문자열이어야 합니다.")
    text = value.strip()
    if required and not text:
        raise RequestError(f"{field}을(를) 입력하세요.")
    if len(text) > maximum:
        raise RequestError(f"{field}이(가) 너무 깁니다.")
    return text


def _history(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_HISTORY_MESSAGES:
        raise RequestError("대화 기록 형식이 올바르지 않습니다.")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise RequestError("대화 기록 형식이 올바르지 않습니다.")
        result.append({"role": item["role"], "content": _bounded_text(item.get("content"), "대화 내용", 2_000)})
    return result


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    if handler.headers.get_content_type() != "application/json":
        raise RequestError("application/json 요청을 보내세요.")
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
        raise RequestError("요청 본문 길이가 올바르지 않습니다.")
    length = int(raw_length)
    if length > MAX_BODY_BYTES:
        raise RequestError("요청 본문이 너무 큽니다.")
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError("JSON 요청 본문이 올바르지 않습니다.") from None
    if not isinstance(value, dict):
        raise RequestError("JSON 객체를 보내세요.")
    return value


def make_handler(service: Any, static_dir: Path):
    """Create a handler class around an injectable service (also used by HTTP tests)."""
    static_dir = static_dir.resolve()

    class WebHandler(BaseHTTPRequestHandler):
        server_version = "YouthPolicyWeb/1.0"

        def log_message(self, format: str, *args) -> None:  # avoid request/body leakage to stdout
            return

        def _send_json(self, status: int, value: dict) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _run(self, action) -> None:
            try:
                self._send_json(HTTPStatus.OK, action())
            except RequestError as error:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))
            except NotFoundError as error:
                self._error(HTTPStatus.NOT_FOUND, "not_found", str(error))
            except HostedQuotaError as error:
                self._error(HTTPStatus.TOO_MANY_REQUESTS, "usage_limit", str(error))
            except (ValueError, RuntimeError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "서비스를 준비하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            except Exception:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path.startswith("/api/"):
                self._run(lambda: self._get_api(parsed.path, parse_qs(parsed.query, keep_blank_values=True)))
                return
            self._static(parsed.path)

        def _get_api(self, path: str, query: dict[str, list[str]]) -> dict:
            def one(name: str) -> str | None:
                values = query.get(name, [])
                if len(values) > 1:
                    raise RequestError(f"{name} 값이 올바르지 않습니다.")
                return values[0] if values else None

            if path == "/api/bootstrap":
                return service.bootstrap()
            if path == "/api/policies":
                keyword = _bounded_text(one("q") or "", "검색어", MAX_QUERY_LENGTH, required=False)
                category = _bounded_text(one("category") or "", "분야", 30, required=False)
                return service.list_policies(keyword, category, _integer(one("page"), "page", 1, 1, 10_000),
                                             _integer(one("limit"), "limit", 12, 1, 48))
            prefix = "/api/policies/"
            if path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
                return service.detail(unquote(path[len(prefix):]))
            raise NotFoundError("API 경로를 찾을 수 없습니다.")

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.path not in {"/api/discover", "/api/chat"}:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "API 경로를 찾을 수 없습니다.")
                return
            self._run(lambda: self._post_api(parsed.path))

        def _post_api(self, path: str) -> dict:
            body = _json_body(self)
            if path == "/api/discover":
                return service.discover(_bounded_text(body.get("query"), "query", MAX_QUERY_LENGTH))
            return service.chat(_bounded_text(body.get("policy_id"), "policy_id", 120),
                                _bounded_text(body.get("question"), "question", MAX_QUESTION_LENGTH),
                                _history(body.get("history", [])))

        def _static(self, raw_path: str) -> None:
            decoded = unquote(raw_path)
            relative = "index.html" if decoded in {"", "/"} else decoded.lstrip("/")
            path = PurePosixPath(relative)
            if (path.is_absolute() or ".." in path.parts or path.name.startswith(".")
                    or path.suffix.lower() not in STATIC_TYPES):
                self._error(HTTPStatus.NOT_FOUND, "not_found", "파일을 찾을 수 없습니다.")
                return
            candidate = (static_dir / Path(*path.parts)).resolve()
            if static_dir not in candidate.parents or not candidate.is_file():
                self._error(HTTPStatus.NOT_FOUND, "not_found", "파일을 찾을 수 없습니다.")
                return
            body = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", STATIC_TYPES[path.suffix.lower()])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return WebHandler


def create_server(service: Any | None = None, static_dir: Path | None = None,
                  host: str = "127.0.0.1", port: int = 8520) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service or PolicyService(), static_dir or ROOT / "web"))


def main() -> None:
    parser = argparse.ArgumentParser(description="청년의 다음 로컬 웹 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8520)
    args = parser.parse_args()
    server = create_server(host=args.host, port=args.port)
    print(f"청년의 다음: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
