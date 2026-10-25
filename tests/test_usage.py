import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from youth_policy.config import Settings
from youth_policy.llm import Model, embeddings
from youth_policy.usage import (
    HostedConfigError,
    HostedQuotaError,
    HostedUsageQuota,
    public_status,
    validate_free_config,
)


class UsageGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def _hosted_env(self, **extra):
        values = {
            "HOSTED_DEMO": "1",
            "FREE_TIER_CONFIRMED": "true",
            "LLM_API_KEY": "hosted-test-key",
        }
        values.update(extra)
        return values

    def _chat_module(self, chat):
        constructor = Mock(return_value=chat)
        return constructor, {"langchain_openai": SimpleNamespace(ChatOpenAI=constructor)}

    def test_hosted_unconfirmed_configuration_blocks_before_chat_constructor(self):
        constructor, module = self._chat_module(Mock())
        with patch.dict(os.environ, {"HOSTED_DEMO": "1", "LLM_API_KEY": "key"}, clear=True), patch.dict(sys.modules, module):
            with self.assertRaises(HostedConfigError):
                Model(self.settings)
        constructor.assert_not_called()

    def test_hosted_allowlist_rejects_wrong_provider_model_and_embedding(self):
        with patch.dict(os.environ, self._hosted_env(), clear=True):
            for invalid in (
                replace(self.settings, llm_base_url="http://generativelanguage.googleapis.com/v1beta/openai/"),
                replace(self.settings, llm_model="gpt-anything"),
                replace(self.settings, embedding_provider="api"),
            ):
                with self.assertRaises(HostedConfigError):
                    validate_free_config(invalid)

    def test_hosted_uses_only_llm_api_key_and_no_provider_retry(self):
        chat = Mock()
        chat.invoke.return_value = SimpleNamespace(content="ok")
        constructor, module = self._chat_module(chat)
        with patch.dict(os.environ, self._hosted_env(GMS_KEY="fallback-must-not-be-used"), clear=True), patch.dict(sys.modules, module):
            model = Model(self.settings)
            self.assertEqual(model.text("system", {"question": "청년 지원"}), "ok")
        self.assertEqual(constructor.call_args.kwargs["api_key"], "hosted-test-key")
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertEqual(constructor.call_args.kwargs["timeout"], 45)
        self.assertEqual(constructor.call_args.kwargs["max_tokens"], 4096)
        self.assertEqual(chat.invoke.call_count, 1)

    def test_failed_hosted_invoke_still_consumes_an_attempt(self):
        chat = Mock()
        chat.invoke.side_effect = RuntimeError("provider unavailable")
        constructor, module = self._chat_module(chat)
        with patch.dict(os.environ, self._hosted_env(), clear=True), patch.dict(sys.modules, module):
            model = Model(self.settings)
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                model.text("system", {"question": "x"})
        db = sqlite3.connect(Path(self.tempdir.name) / "hosted_usage.sqlite3")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM hosted_attempts").fetchone()[0], 1)
        finally:
            db.close()
        constructor.assert_called_once()
        chat.invoke.assert_called_once()

    def test_local_behavior_keeps_gms_fallback_and_retry_defaults(self):
        chat = Mock()
        chat.invoke.return_value = SimpleNamespace(content="ok")
        constructor, module = self._chat_module(chat)
        with patch.dict(os.environ, {"GMS_KEY": "local-fallback"}, clear=True), patch.dict(sys.modules, module):
            model = Model(self.settings)
            self.assertEqual(model.text("system", {"x": 1}), "ok")
        self.assertEqual(constructor.call_args.kwargs["api_key"], "local-fallback")
        self.assertEqual(constructor.call_args.kwargs["timeout"], 60)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 2)
        self.assertNotIn("max_tokens", constructor.call_args.kwargs)

    def test_quota_limits_are_atomic_and_reset_by_time_window(self):
        database = Path(self.tempdir.name) / "quota.sqlite3"
        now = [1_700_000_000.0]
        quota = HostedUsageQuota(database, clock=lambda: now[0], daily_limit=2, minute_limit=1)
        quota.consume()
        with self.assertRaises(HostedQuotaError):
            quota.consume()
        now[0] += 61
        quota.consume()
        with self.assertRaises(HostedQuotaError):
            quota.consume()

    def test_daily_quota_resets_at_next_utc_day(self):
        database = Path(self.tempdir.name) / "daily.sqlite3"
        now = [1_700_000_000.0]
        quota = HostedUsageQuota(database, clock=lambda: now[0], daily_limit=1, minute_limit=12)
        quota.consume()
        with self.assertRaises(HostedQuotaError):
            quota.consume()
        now[0] += 86_400
        quota.consume()

    def test_rolling_minute_limit_survives_utc_midnight(self):
        database = Path(self.tempdir.name) / "midnight.sqlite3"
        midnight = datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp()
        now = [midnight - 30]
        quota = HostedUsageQuota(database, clock=lambda: now[0], daily_limit=60, minute_limit=1)
        quota.consume()
        now[0] = midnight + 30
        with self.assertRaises(HostedQuotaError):
            quota.consume()

    def test_quota_concurrency_never_exceeds_minute_limit(self):
        database = Path(self.tempdir.name) / "concurrent.sqlite3"
        barrier = threading.Barrier(21)

        def attempt():
            barrier.wait()
            try:
                HostedUsageQuota(database, clock=lambda: 1_700_000_000.0).consume()
                return True
            except HostedQuotaError:
                return False

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(attempt) for _ in range(20)]
            barrier.wait()
            allowed = sum(future.result() for future in futures)
        self.assertEqual(allowed, 12)

    def test_public_status_is_generic_and_does_not_expose_key(self):
        with patch.dict(os.environ, self._hosted_env(LLM_API_KEY="secret-value"), clear=True):
            status = public_status(self.settings)
        self.assertTrue(status["ai_enabled"])
        self.assertNotIn("secret-value", status["message"])

    def test_hosted_payload_limit_blocks_before_invoke(self):
        chat = Mock()
        constructor, module = self._chat_module(chat)
        with patch.dict(os.environ, self._hosted_env(), clear=True), patch.dict(sys.modules, module):
            model = Model(self.settings)
            with self.assertRaisesRegex(ValueError, "너무 큽니다"):
                model.text("system", {"question": "가" * 120_001})
        chat.invoke.assert_not_called()

    def test_hosted_local_embedding_cache_is_used_only_when_configured(self):
        created_paths = []

        class FakeEmbeddings:
            REVISION = "revision"

            def __init__(self, path):
                created_paths.append(path)

        module = {"youth_policy.local_embeddings": SimpleNamespace(LocalEmbeddings=FakeEmbeddings)}
        cache = Path(self.tempdir.name) / "packaged-assets"
        with patch.dict(os.environ, self._hosted_env(LOCAL_EMBEDDING_CACHE=str(cache)), clear=True), patch.dict(sys.modules, module):
            embeddings(self.settings)
        self.assertEqual(created_paths, [cache / "revision"])


if __name__ == "__main__":
    unittest.main()
