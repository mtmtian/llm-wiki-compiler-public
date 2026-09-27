"""Tests for the single-machine v1 proposal importer used during v2 migration."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json, save_json
from exchange import export_result, import_pending, settings
from replica import initialize_baseline, publish_record, read_records
from replica_records import validate_config
from queue_worker import process_queue


class LegacyImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exchange = self.root / "exchange"
        self.projects = {"growth": {"label": "Growth", "pages": []}}

    def tearDown(self):
        self.temp.cleanup()

    def config(self, machine, importer="peer-a", publish=True, intake=True, field=True):
        exchange = {"protocolVersion": 2, "root": str(self.exchange), "participants": ["peer-a", "peer"]}
        if field:
            exchange["legacyImporterMachineId"] = importer
        return {"enabled": True, "intakeEnabled": intake, "publishEnabled": publish,
                "machineId": machine, "stateDir": str(self.root / machine),
                "wikiRoot": str(self.root / machine / "replica/current"),
                "sharedWikiRoot": str(self.root / "shared"), "projects": self.projects,
                "exchange": exchange}

    def legacy_submission(self):
        old = self.config("peer", publish=False, field=False)
        old["exchange"] = {"root": str(self.exchange), "publisherMachineId": "peer-a", "participants": ["peer-a", "peer"]}
        job = {"id": "legacy-job", "projectId": "growth", "projectLabel": "Growth",
               "createdAt": "2026-09-16T00:00:00Z"}
        evidence = {"id": "e", "kind": "user", "text": "Keep the experiment log",
                    "sha256": digest("Keep the experiment log"), "locator": "turn:e",
                    "observedAt": "2026-09-16T00:00:00Z"}
        result = {"status": "submitted", "publishedPageIds": [], "reviewCount": 0,
                  "contribution": {"claims": [{"text": evidence["text"], "evidenceId": "e"}],
                                    "evidence": [evidence]}}
        return export_result(old, job, result)

    def test_only_declared_v2_importer_queues_legacy_submission(self):
        sent = self.legacy_submission()
        importer, writer = self.config("peer-a"), self.config("peer")
        with patch("replica.read_records", return_value=[]):
            self.assertEqual(import_pending(writer), 0)
            self.assertEqual(import_pending(importer), 1)
        queued = load_json(next((self.root / "peer-a/queue").glob("*.json")))
        self.assertEqual(queued["id"], "exchange-" + sent["submissionId"])

    def test_non_importer_legacy_queue_never_syncs_or_invokes_model(self):
        config = self.config("peer")
        job = {"id": "exchange-" + "a" * 64, "projectId": "growth", "sessionId": "legacy",
               "prompt": "review", "evidence": [], "allowedPageIds": []}
        save_json(self.root / "peer/queue/legacy.json", job)
        calls = []
        with patch("replica.sync_replica", side_effect=AssertionError("non-importer synced")):
            result = process_queue(config, lambda *args: calls.append(args), clock=lambda: datetime.now(timezone.utc))
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["reason"], "replica-sync")
        self.assertFalse(calls)
        audit = next((self.root / "peer/batches").glob("*.json"))
        self.assertEqual(load_json(audit)["error"], "LegacyImporterError")

    def test_publish_gate_rejects_old_queue_on_non_importer(self):
        config = self.config("peer")
        job = {"id": "exchange-" + "b" * 64, "projectId": "growth", "projectLabel": "Growth",
               "createdAt": "2026-09-16T00:00:00Z", "basisRecordIds": []}
        result = {"status": "submitted", "contribution": {"claims": [], "evidence": []}}
        with self.assertRaisesRegex(ValueError, "designated importer"):
            publish_record(config, job, result)

    def test_importer_publishes_one_v2_record_and_peer_cannot_duplicate_legacy_job(self):
        shared = self.root / "shared/wiki/concepts"
        (self.root / "shared/sources").mkdir(parents=True)
        shared.mkdir(parents=True)
        (self.root / "shared/sources/base.md").write_text("base", encoding="utf-8")
        (self.root / "shared/.llmwiki").mkdir()
        (self.root / "shared/.llmwiki/config.json").write_text("{}", encoding="utf-8")
        importer, peer = self.config("peer-a"), self.config("peer")
        initialize_baseline(importer)
        evidence = {"id": "e", "kind": "user", "text": "Keep the experiment log",
                    "sha256": digest("Keep the experiment log"), "locator": "turn:e",
                    "observedAt": "2026-09-16T00:00:00Z"}
        claim = {"text": evidence["text"], "quote": evidence["text"], "evidenceId": "e",
                 "useWhen": "running experiments", "title": "Experiment log", "topic": "growth",
                 "slug": "experiment-log", "targetPageId": None, "kind": "decision", "status": "decided",
                 "rationale": "reviewed quote"}
        self.legacy_submission()
        with patch("replica.read_records", return_value=[]):
            self.assertEqual(import_pending(peer), 0)
            self.assertEqual(import_pending(importer), 1)
        job = load_json(next((self.root / "peer-a/queue").glob("*.json")))
        result = {"status": "submitted", "publishedPageIds": [],
                  "contribution": {"claims": [claim], "evidence": [evidence]}}
        self.assertEqual(import_pending(peer), 0)
        publish_record(importer, job, result)
        self.assertEqual(import_pending(peer), 0)
        with self.assertRaisesRegex(ValueError, "designated importer"):
            publish_record(peer, job, result)
        self.assertEqual(len(read_records(importer)), 1)

    def test_saved_legacy_result_on_peer_is_held_without_model_or_publication(self):
        config = self.config("peer")
        path = self.root / "peer/batches/legacy.json"
        save_json(path, {"batchId": "legacy", "status": "result-ready", "queueFiles": [],
                         "job": {"id": "exchange-" + "d" * 64, "projectId": "growth"},
                         "result": {"status": "submitted", "contribution": {}}})
        calls = []
        result = process_queue(config, lambda *args: calls.append(args))
        audit = load_json(path)
        self.assertEqual(result["finalizeErrors"], 1)
        self.assertEqual(audit["status"], "finalize-retry")
        self.assertEqual(audit["error"], "LegacyImporterError")
        self.assertFalse(calls)
        self.assertFalse((self.exchange / "v2/publications").exists())

    def test_invalid_importer_and_missing_contributor_declaration_fail(self):
        for invalid in ("unknown", None, [], "../peer-a"):
            with self.subTest(importer=invalid):
                with self.assertRaisesRegex(ValueError, "legacyImporterMachineId"):
                    settings(self.config("peer-a", importer=invalid))
                with self.assertRaisesRegex(ValueError, "legacyImporterMachineId"):
                    validate_config(self.config("peer-a", importer=invalid))
        with self.assertRaisesRegex(ValueError, "legacyImporterMachineId"):
            settings(self.config("peer", publish=False, intake=True, field=False))
        with self.assertRaisesRegex(ValueError, "legacyImporterMachineId"):
            validate_config(self.config("peer", publish=False, intake=True, field=False))
        self.assertEqual(import_pending(self.config("peer-a", field=False)), 0)


if __name__ == "__main__":
    unittest.main()
