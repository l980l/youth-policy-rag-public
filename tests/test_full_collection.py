import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from youth_policy.config import Settings
from youth_policy.domain import FIELDS, normalize
from youth_policy.storage import read_json


class FullCollectionCLITests(unittest.TestCase):
    def test_default_collection_has_no_100_item_cap(self):
        from youth_policy.cli import main
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            policy = normalize({**{key: "" for key in FIELDS}, "plcyNo": "test", "plcyNm": "가상 정책"})
            client = Mock()
            client.collect_all.return_value = [policy]
            client.collection_summary = {"api_total": 1, "unique_count": 1, "complete": True}
            with patch("sys.argv", ["cli", "collect"]), patch("youth_policy.cli.load_settings", return_value=settings), \
                    patch("youth_policy.collector.YouthClient", return_value=client), patch("builtins.print"):
                main()
            client.collect_all.assert_called_once()
            client.collect.assert_not_called()
            client.detail.assert_not_called()
            report = read_json(settings.data_dir / "collection_report.json")
            self.assertEqual(report["mode"], "all")
            self.assertTrue(report["complete"])
            self.assertEqual(read_json(settings.data_dir / "policies.json")[0]["evidence_quality"], "limited")


if __name__ == "__main__":
    unittest.main()
