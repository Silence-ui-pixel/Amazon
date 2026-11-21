import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters.xlsx_simple import build_xlsx
from app.domain.errors import DomainError
from app.services.source_exports import SourceExportService


class FakeAba:
    def __init__(self, payload):
        self.payload = payload
        self.download_calls = []
        self.fetch_calls = []

    def download_original(self, request, target):
        self.download_calls.append((request, target))
        if isinstance(self.payload, Exception):
            raise self.payload
        filename, raw = self.payload
        target.write_bytes(raw)
        return filename, raw

    def fetch(self, *args, **kwargs):
        self.fetch_calls.append((args, kwargs))
        raise AssertionError("ABA source exports must not be generated from API rows")


class SourceExportPlatformDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = build_xlsx(
            [
                ["st-search-term", "st-click-share-1", "st-conversion-share-1"],
                ["adjustable dumbbell rack", "0.4", "0.2"],
            ],
            sheet="ABA Search Terms",
        )
        self.aba = FakeAba(("platform-report.xlsx", self.raw))
        self.repository = SimpleNamespace(
            storage_root=self.root,
            get_run=lambda project, run: None,
        )
        self.service = SourceExportService(self.repository, self.aba)
        self.item = self.service.store.create(
            "project", "run", "aba_forward", "owner", "adjustable dumbbell rack",
            {"query": "adjustable dumbbell rack", "week_end": "2026-11-07", "country": "us"},
        )
        self.item["device_id"] = "device-1"

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_aba_export_archives_platform_file_and_builds_calculated_table(self):
        self.service.execute(self.item)

        self.assertEqual(self.aba.fetch_calls, [])
        self.assertEqual(len(self.aba.download_calls), 1)
        self.assertEqual(self.item["source_type"], "platform_download")
        self.assertEqual(self.item["status"], "completed")
        self.assertEqual(self.item["sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(self.service.store.file(self.item, True), self.raw)
        self.assertTrue(self.service.store.file(self.item, False).startswith(b"PK"))

    def test_non_excel_agent_payload_is_not_archived_as_platform_original(self):
        self.aba.payload = ("report.xlsx", b"{\"data\": []}")

        self.service.execute(self.item)

        self.assertEqual(self.item["status"], "download_failed")
        self.assertEqual(self.item["error_code"], "ABA_PLATFORM_FILE_INVALID")
        self.assertEqual(self.item["sha256"], "")

    def test_completed_legacy_api_export_can_retry_platform_download(self):
        self.item.update(
            status="completed", source_type="api_generated", filename="old-api.xlsx",
            sha256="legacy", tables=[{"name": "old", "headers": ["x"], "rows": [["y"]]}],
        )
        self.service.store.save(self.item)
        with patch.object(self.service, "_submit") as submit:
            retried = self.service.retry("project", "run", self.item["id"])

        self.assertEqual(retried["status"], "queued")
        submit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
