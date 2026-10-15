import json
import unittest
from unittest.mock import MagicMock, patch

from data.seed import index, upload_blobs


class LegacySeedIsolationTests(unittest.TestCase):
    def test_index_rejects_insurance_before_settings_or_search_clients(self):
        manifest = MagicMock()
        manifest.read_text.return_value = json.dumps([{"business_domain": "insurance"}])
        with patch.object(index, "MANIFEST", manifest), \
                patch.object(index, "get_settings") as settings, \
                patch.object(index, "SearchIndexClient") as client:
            with self.assertRaisesRegex(RuntimeError, "retail index must remain unchanged"):
                index.main()
            settings.assert_not_called()
            client.assert_not_called()

    def test_uploader_rejects_mixed_corpus_before_blob_authentication(self):
        manifest = MagicMock()
        manifest.exists.return_value = True
        manifest.read_text.return_value = json.dumps([
            {"business_domain": "retail"}, {"business_domain": "insurance"},
        ])
        with patch.object(upload_blobs, "_MANIFEST", manifest), \
                patch.object(upload_blobs, "_blob_service") as service:
            with self.assertRaisesRegex(RuntimeError, "retail seed container must remain unchanged"):
                upload_blobs.main()
            service.assert_not_called()

    def test_unknown_domains_cannot_reach_search(self):
        manifest = MagicMock()
        manifest.read_text.return_value = json.dumps([{"business_domain": "Insurance"}])
        with patch.object(index, "MANIFEST", manifest), \
                patch.object(index, "get_settings") as settings:
            with self.assertRaisesRegex(RuntimeError, "historical retail manifests only"):
                index.main()
            settings.assert_not_called()

    def test_explicit_retail_and_historical_manifests_keep_existing_index_path(self):
        for entry in [{"business_domain": "retail"}, {}]:
            manifest = MagicMock()
            manifest.read_text.return_value = json.dumps([entry])
            with self.subTest(entry=entry), patch.object(index, "MANIFEST", manifest), \
                    patch.object(index, "get_settings", side_effect=RuntimeError("retail settings reached")):
                with self.assertRaisesRegex(RuntimeError, "retail settings reached"):
                    index.main()

    def test_explicit_retail_keeps_existing_blob_path(self):
        manifest = MagicMock()
        manifest.exists.return_value = True
        manifest.read_text.return_value = json.dumps([{"business_domain": "retail"}])
        with patch.object(upload_blobs, "_MANIFEST", manifest), \
                patch.object(upload_blobs, "_blob_service", side_effect=RuntimeError("retail blob path reached")):
            with self.assertRaisesRegex(RuntimeError, "retail blob path reached"):
                upload_blobs.main()


if __name__ == "__main__":
    unittest.main()
