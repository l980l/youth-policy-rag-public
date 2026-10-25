"""Vercel-facing API handler that keeps expensive service setup lazy."""
from __future__ import annotations

import threading
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .web import make_handler


class _LazyService:
    """Initialize the process-local service only when an API route needs it."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._service: Any | None = None
        self._lock = threading.Lock()

    def _get_service(self) -> Any:
        if self._service is None:
            with self._lock:
                if self._service is None:
                    try:
                        service = self._factory()
                    except Exception as error:
                        # web.make_handler intentionally renders RuntimeError as its
                        # generic, non-sensitive 503 response.
                        raise RuntimeError("service initialization failed") from error
                    if service is None:
                        raise RuntimeError("service initialization failed")
                    self._service = service
        return self._service

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_service(), name)


def make_serverless_handler(factory: Callable[[], Any]):
    """Build an API-only handler suitable for Vercel's Python runtime.

    The underlying web handler retains the public API validation and error
    semantics.  Its static-file path is explicitly disabled because Vercel
    serves the published frontend separately from this serverless function.
    """
    lazy_service = _LazyService(factory)
    WebHandler = make_handler(lazy_service, Path(__file__).parent)

    class ServerlessHandler(WebHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/api/health":
                self._send_json(HTTPStatus.OK, {"status": "ok"})
                return
            super().do_GET()

        def _static(self, raw_path: str) -> None:
            del raw_path
            self._error(HTTPStatus.NOT_FOUND, "not_found", "파일을 찾을 수 없습니다.")

    return ServerlessHandler
