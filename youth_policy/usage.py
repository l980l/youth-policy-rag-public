"""Hosted-demo safeguards.

The quota is scoped to this runtime's SQLite storage.  It is neither an
account-global limit nor a promise that a provider will not charge for a
request; the deployment operator remains responsible for provider billing.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ALLOWED_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
ALLOWED_MODELS = {"gemini-3.5-flash-lite", "gemini-2.5-flash-lite"}
_CONFIG_MESSAGE = "호스팅 AI 기능 설정을 확인해 주세요."
_QUOTA_MESSAGE = "무료 체험 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."


class HostedConfigError(ValueError):
    """Raised when a hosted deployment has not been explicitly allow-listed."""


class HostedQuotaError(RuntimeError):
    """Safe, user-displayable error for the hosted-demo request limit."""

    def __init__(self) -> None:
        super().__init__(_QUOTA_MESSAGE)


def is_hosted(environ: dict[str, str] | None = None) -> bool:
    """Return whether this process is running in the constrained demo mode."""
    values = os.environ if environ is None else environ
    return values.get("VERCEL") == "1" or values.get("HOSTED_DEMO") == "1"


def _normalized_allowed_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (AttributeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "generativelanguage.googleapis.com"
        and port is None
        and not parsed.username
        and not parsed.password
        and parsed.path.rstrip("/") == "/v1beta/openai"
        and not parsed.query
        and not parsed.fragment
    )


def validate_free_config(settings, environ: dict[str, str] | None = None) -> None:
    """Reject unsafe hosted model configuration before a provider can be called."""
    if not is_hosted(environ):
        return
    values = os.environ if environ is None else environ
    confirmed = values.get("FREE_TIER_CONFIRMED", "").strip().lower() == "true"
    if (
        not confirmed
        or not _normalized_allowed_base_url(settings.llm_base_url)
        or settings.llm_model not in ALLOWED_MODELS
        or settings.embedding_provider != "local"
    ):
        raise HostedConfigError(_CONFIG_MESSAGE)


def public_status(settings=None, environ: dict[str, str] | None = None) -> dict[str, object]:
    """Return a display-safe hosting status without exposing secret configuration."""
    if not is_hosted(environ):
        return {"ai_enabled": True, "message": "AI 응답은 제공자 설정과 사용 한도에 따라 달라질 수 있습니다."}
    values = os.environ if environ is None else environ
    try:
        if settings is not None:
            validate_free_config(settings, environ)
        elif values.get("FREE_TIER_CONFIRMED", "").strip().lower() != "true":
            raise HostedConfigError(_CONFIG_MESSAGE)
        if not values.get("LLM_API_KEY", "").strip():
            raise HostedConfigError(_CONFIG_MESSAGE)
    except HostedConfigError:
        return {"ai_enabled": False, "message": "AI 체험 기능은 현재 준비 중입니다."}
    return {"ai_enabled": True, "message": "AI 체험은 사용 한도 내에서 제공됩니다."}


class HostedUsageQuota:
    """Atomically meter hosted invocations in a runtime-local SQLite database."""

    def __init__(
        self,
        database: Path | str,
        *,
        clock=time.time,
        daily_limit: int = 60,
        minute_limit: int = 12,
    ) -> None:
        self.database = Path(database)
        self.clock = clock
        self.daily_limit = daily_limit
        self.minute_limit = minute_limit

    def consume(self) -> None:
        """Reserve one request before invocation, or raise without invoking it."""
        now = float(self.clock())
        day_start = datetime.fromtimestamp(now, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS hosted_attempts (created_at REAL NOT NULL)")
            # Retain both the current UTC day's total and the last rolling minute.
            # Around midnight, the latter can include attempts from yesterday.
            connection.execute("DELETE FROM hosted_attempts WHERE created_at < ?", (min(day_start, now - 60),))
            minute_count = connection.execute(
                "SELECT COUNT(*) FROM hosted_attempts WHERE created_at >= ?", (now - 60,)
            ).fetchone()[0]
            daily_count = connection.execute(
                "SELECT COUNT(*) FROM hosted_attempts WHERE created_at >= ?", (day_start,)
            ).fetchone()[0]
            if minute_count >= self.minute_limit or daily_count >= self.daily_limit:
                connection.execute("ROLLBACK")
                raise HostedQuotaError()
            connection.execute("INSERT INTO hosted_attempts (created_at) VALUES (?)", (now,))
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
