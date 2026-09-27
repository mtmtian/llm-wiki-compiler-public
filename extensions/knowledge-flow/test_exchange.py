"""Two isolated hosts share immutable proposals, never process each other's state."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from common import digest, load_json, save_json
from exchange import canonical, exchange_counts, export_result, import_pending, settings, write_receipt
from unittest.mock import patch
import hooks


class ExchangeTests(unittest.TestCase):
    """Exercise transport with separate private states and a shared file root."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"enabled": True, "intakeEnabled": True, "publishEnabled": False,
                       "machineId": "a", "wikiRoot": str(self.root / "wiki"),
                       "stateDir": str(self.root / "a"), "projects": {"project": {"pages": []}},
                       "exchange": {"root": str(self.root / "exchange"),
                                    "publisherMachineId": "b", "participants": ["a", "b"]}}
        self.publisher = copy.deepcopy(self.config)
        self.publisher.update(machineId="b", publishEnabled=True, stateDir=str(self.root / "b"))
        self.job = {"id": "j", "projectId": "project", "projectLabel": "Project", "createdAt": "2026-09-15T00:00:00Z"}
        evidence = {"id": "e1", "kind": "user", "text": "Use IDs", "sha256": digest("Use IDs"), "locator": "turn:t"}
        self.result = {"status": "submitted", "publishedPageIds": [], "reviewCount": 0,
                       "contribution": {"claims": [{"text": "Use IDs", "evidenceId": "e1"}], "evidence": [evidence]}}

    def tearDown(self):
        self.temp.cleanup()

    def test_contributor_exports_only_immutable_packet_and_retry_is_idempotent(self):
        first = export_result(self.config, self.job, self.result)
        self.assertEqual(export_result(self.config, self.job, self.result), first)
        self.assertNotIn("contribution", first)
        self.assertEqual(len(list((self.root / "exchange/submissions/a").glob("*.json"))), 1)
        self.assertFalse((self.root / "wiki").exists())
        self.assertEqual(import_pending(self.config), 0)

    def test_contributor_counts_only_its_submissions_and_matching_receipts(self):
        sent = export_result(self.config, self.job, self.result)
        save_json(self.root / "exchange/submissions/b/other.json", {})
        folders = []
        original = Path.glob
        def tracked(folder, pattern):
            folders.append((folder, pattern))
            return original(folder, pattern)
        with patch.object(Path, "glob", tracked):
            self.assertEqual(exchange_counts(self.config), {"submitted": 1, "unreceipted": 1})
        self.assertEqual(folders, [(self.root / "exchange/submissions/a", "*.json")])
        save_json(self.root / "exchange/receipts" / (sent["submissionId"] + ".json"), {})
        self.assertEqual(exchange_counts(self.config)["unreceipted"], 0)
        self.assertEqual(exchange_counts(self.publisher)["submitted"], 2)

    def test_only_publisher_imports_and_receipt_prevents_replay_on_a_fresh_host(self):
        sent = export_result(self.config, self.job, self.result)
        self.assertEqual(import_pending(self.publisher), 1)
        self.assertEqual(import_pending(self.publisher), 0)
        file = next((self.root / "b/queue").glob("*.json"))
        job = load_json(file)
        self.assertEqual(job["id"], "exchange-" + sent["submissionId"])
        self.assertEqual(job["cwd"], self.config["wikiRoot"])
        self.assertNotIn("lastAssistant", canonical(load_json(next((self.root / "exchange/submissions/a").glob("*.json")))))
        write_receipt(self.publisher, job, {"status": "published", "publishedPageIds": ["concepts/new"], "reviewCount": 0})
        self.publisher["stateDir"] = str(self.root / "b-reinstalled")
        self.assertEqual(import_pending(self.publisher), 0)

    def test_tampered_and_incomplete_packets_never_enter_local_queue(self):
        export_result(self.config, self.job, self.result)
        file = next((self.root / "exchange/submissions/a").glob("*.json"))
        value = load_json(file)
        value["payload"]["projectId"] = "other"
        file.write_text(json.dumps(value))
        self.assertEqual(import_pending(self.publisher), 0)
        file.write_text('{"incomplete":')
        self.assertEqual(import_pending(self.publisher), 0)
        self.assertFalse((self.root / "b/queue").exists())

    def test_undeclared_project_and_assistant_evidence_are_rejected(self):
        self.job["projectId"] = "unknown"
        export_result(self.config, self.job, self.result)
        self.assertEqual(import_pending(self.publisher), 0)
        self.job["projectId"] = "project"
        self.result["contribution"]["evidence"][0]["kind"] = "assistant"
        export_result(self.config, self.job, self.result)
        self.assertEqual(import_pending(self.publisher), 0)

    def test_wrong_machine_cannot_enable_publication(self):
        self.config["publishEnabled"] = True
        with self.assertRaisesRegex(ValueError, "designated"):
            settings(self.config)

    def test_captured_analysis_requires_historical_lesson_on_publisher(self):
        self.result["contribution"]["evidence"][0]["kind"] = "assistant"
        claim = self.result["contribution"]["claims"][0]
        claim.update(kind="lesson", status="historical")
        sent = export_result(self.config, self.job, self.result)
        self.assertEqual(import_pending(self.publisher), 1)
        job = load_json(self.root / "b/queue" / ("exchange-" + sent["submissionId"] + ".json"))
        self.assertEqual(job["evidence"][0]["kind"], "assistant")
        claim.update(kind="fact", status="decided")
        self.job["id"] = "another-job"
        export_result(self.config, self.job, self.result)
        self.assertEqual(import_pending(self.publisher), 0)

    def test_shared_stop_only_queues_without_running_models(self):
        event = {"session_id": "s", "turn_id": "t", "cwd": str(self.root),
                 "hook_event_name": "Stop", "last_assistant_message": ""}
        save_json(hooks.event_path(self.config, event),
                  {"projectId": "project", "prompt": "We must use stable IDs", "createdAt": "2026-09-15"})
        with patch.object(hooks, "process_queue") as process:
            hooks.handle(event, self.config)
            process.assert_not_called()
        self.assertEqual(len(list((self.root / "a/queue").glob("*.json"))), 1)

    def test_completed_local_audit_recovers_missing_receipt(self):
        sent = export_result(self.config, self.job, self.result)
        save_json(self.root / "b/completed" / ("exchange-" + sent["submissionId"] + ".json"),
                  {"status": "needs_review", "reviewCount": 1, "reviewFile": "/private/review.json"})
        self.assertEqual(import_pending(self.publisher), 0)
        receipt = load_json(self.root / "exchange/receipts" / (sent["submissionId"] + ".json"))
        self.assertEqual(receipt["reviewCount"], 1)
        self.assertNotIn("reviewFile", receipt)

    def test_partial_receipt_is_reported_and_retried_after_sync(self):
        sent = export_result(self.config, self.job, self.result)
        receipt = self.root / "exchange/receipts" / (sent["submissionId"] + ".json")
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{")
        self.assertEqual(import_pending(self.publisher), 0)
        self.assertEqual(len(list((self.root / "b/exchange-errors").glob("*.json"))), 1)
        receipt.unlink()
        self.assertEqual(import_pending(self.publisher), 1)
        self.assertEqual(len(list((self.root / "b/exchange-errors").glob("*.json"))), 0)

    def test_transient_worker_error_never_creates_a_final_receipt(self):
        sent = export_result(self.config, self.job, self.result)
        import_pending(self.publisher)
        job = load_json(next((self.root / "b/queue").glob("*.json")))
        write_receipt(self.publisher, job, {"status": "error"})
        receipt = self.root / "exchange/receipts" / (sent["submissionId"] + ".json")
        self.assertFalse(receipt.exists())
        write_receipt(self.publisher, job, {"status": "published", "publishedPageIds": ["concepts/new"]})
        self.assertEqual(load_json(receipt)["status"], "published")

    def test_shared_locator_does_not_expose_local_path_or_session(self):
        self.result["contribution"]["evidence"][0]["locator"] = "/Users/private/project/report.md"
        export_result(self.config, self.job, self.result)
        text = next((self.root / "exchange/submissions/a").glob("*.json")).read_text()
        self.assertNotIn("/Users/private", text)
        self.assertIn("knowledge-evidence://a/", text)


if __name__ == "__main__":
    unittest.main()
