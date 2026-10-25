import json
import tempfile
import unittest
from pathlib import Path

from youth_policy.config import Settings
from youth_policy.hosting import prepare_runtime
from youth_policy.index import fingerprint
from youth_policy.local_embeddings import LocalEmbeddings


class HostingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.settings = Settings(data_dir=self.root / "unused-source-data")
        self.assets = self.root / "runtime_assets"
        self.seed = self.assets / "seed"
        (self.seed / "chroma").mkdir(parents=True)
        (self.seed / "policies.json").write_text("[]")
        (self.seed / "index_settings.json").write_text(json.dumps(fingerprint(self.settings)))
        (self.seed / "chroma" / "chroma.sqlite3").write_bytes(b"small-test-index")
        self.model = self.assets / "models" / LocalEmbeddings.REVISION
        for name in (LocalEmbeddings.MODEL_FILE, LocalEmbeddings.TOKENIZER_FILE):
            path = self.model / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model-asset")
        (self.assets / "manifest.json").write_text(json.dumps({"index_settings": fingerprint(self.settings)}))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_copies_seed_once_to_writable_runtime_and_reuses_it(self):
        temporary_root = self.root / "temporary"
        runtime, model_cache = prepare_runtime(self.assets, self.settings, temporary_root)

        self.assertNotEqual(runtime.data_dir, self.seed)
        self.assertEqual((runtime.data_dir / "policies.json").read_text(), "[]")
        self.assertEqual(model_cache, self.model)
        (runtime.data_dir / "warm-runtime-state").write_text("preserved")

        reused, _ = prepare_runtime(self.assets, self.settings, temporary_root)
        self.assertEqual(reused.data_dir, runtime.data_dir)
        self.assertEqual((reused.data_dir / "warm-runtime-state").read_text(), "preserved")

    def test_rejects_fingerprint_mismatch_before_copying(self):
        manifest = self.assets / "manifest.json"
        manifest.write_text(json.dumps({"index_settings": {"wrong": "settings"}}))

        with self.assertRaisesRegex(ValueError, "검색 설정"):
            prepare_runtime(self.assets, self.settings, self.root / "temporary")

    def test_rejects_incomplete_seed_before_copying(self):
        (self.seed / "chroma" / "chroma.sqlite3").unlink()

        with self.assertRaisesRegex(ValueError, "배포 데이터"):
            prepare_runtime(self.assets, self.settings, self.root / "temporary")

    def test_rejects_missing_packaged_embedding_asset(self):
        (self.model / LocalEmbeddings.TOKENIZER_FILE).unlink()

        with self.assertRaisesRegex(ValueError, "검색 모델"):
            prepare_runtime(self.assets, self.settings, self.root / "temporary")


if __name__ == "__main__":
    unittest.main()
