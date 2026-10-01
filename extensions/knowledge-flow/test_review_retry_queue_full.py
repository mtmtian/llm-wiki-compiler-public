"""A batch refused by a full review queue can be reprocessed like a held review.

Given a completed batch whose only hold record is the ``review queue is full``
audit, an explicit retry reuses its frozen evidence, anchors on that audit and
resolves it after a genuine outcome, without weakening any other retry check.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from common import load_json, save_json
from queue_worker import process_queue
from review_capacity import QUEUE_FULL_ERROR
from review_retry import retry_review


class QueueFullRetryTests(unittest.TestCase):
    """Exercise real queue finalization with an external model boundary fake."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "projects": {"p": {"pages": []}}, "maxDailyJobs": 100,
                       "maxPendingPerProject": 2}
        job = {"id": "batch-full", "projectId": "p", "sessionId": "session",
               "sourceJobIds": ["turn-full"], "evidence": [{"id": "e", "text": "original"}],
               "sessionContext": {"version": 1, "revision": 1, "summary": "", "topicPageIds": [],
                                  "evidence": []}, "allowedPageIds": []}
        refused = {"status": "needs_review", "publishedPageIds": [], "reviewCount": 1, "error": QUEUE_FULL_ERROR}
        save_json(self.state / "batches/batch-full.json", {"batchId": "batch-full", "status": "completed",
                  "job": job, "result": refused})
        save_json(self.state / "audit/batch-full.json", {**refused, "jobId": "batch-full", "projectId": "p"})
        save_json(self.state / "completed/turn-full.json", {"status": "needs_review"})

    def retry(self, dry_run=False, config=None):
        return retry_review(config or self.config, "batch-full", dry_run=dry_run, clock=lambda: self.now)

    def drain(self, result):
        return process_queue(self.config, lambda *args: result, clock=lambda: self.now)

    def test_dry_run_accepts_the_audit_anchor_without_writing(self):
        """Given a queue-full hold, dry-run is ready and changes nothing."""
        before = sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*.json"))
        self.assertEqual(self.retry(dry_run=True)["status"], "ready")
        self.assertEqual(before, sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*.json")))

    def test_retry_outcome_resolves_the_hold_and_keeps_its_audit(self):
        """A genuine outcome archives the hold; the refusal audit and batch stay frozen."""
        frozen = (self.state / "batches/batch-full.json").read_bytes()
        audit = (self.state / "audit/batch-full.json").read_bytes()
        queued = self.retry()
        result = self.drain({"status": "empty", "publishedPageIds": []})
        self.assertEqual(result["processed"], 1)
        archive = load_json(self.state / "resolved/batch-full.json")
        self.assertEqual((archive["action"], archive["retryJobId"]), ("reprocessed", queued["retryJobId"]))
        self.assertEqual((self.state / "batches/batch-full.json").read_bytes(), frozen)
        self.assertEqual((self.state / "audit/batch-full.json").read_bytes(), audit)

    def test_changed_audit_is_rejected_before_any_model_call(self):
        """The audit is the frozen anchor: changing it stops the retry."""
        self.retry()
        save_json(self.state / "audit/batch-full.json", {"status": "needs_review", "error": QUEUE_FULL_ERROR,
                                                         "jobId": "batch-full", "projectId": "p", "x": 1})
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(calls, [])
        self.assertEqual(result["processed"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json", {}).get("used", 0), 0)
        self.assertFalse((self.state / "resolved/batch-full.json").exists())

    def test_hold_without_review_or_queue_full_audit_is_still_rejected(self):
        """Only the queue-full refusal may stand in for a missing review."""
        save_json(self.state / "audit/batch-full.json", {"status": "needs_review", "jobId": "batch-full",
                                                         "projectId": "p"})
        with self.assertRaises(ValueError):
            self.retry(dry_run=True)
        (self.state / "audit/batch-full.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.retry(dry_run=True)

    def test_full_project_refuses_to_stage_because_the_retry_frees_no_slot(self):
        """Staging waits for room so a retry cannot just be refused again."""
        for name in ("old-1", "old-2"):
            save_json(self.state / "review" / (name + ".json"), {"jobId": name, "projectId": "p"})
        with self.assertRaises(ValueError):
            self.retry()
        self.assertEqual(list((self.state / "queue").glob("*.json")), [])

    def test_refused_again_keeps_the_original_hold_without_finalize_errors(self):
        """Given a race that refills the project, a repeated refusal leaves the hold as it was."""
        self.retry()
        result = self.drain({"status": "needs_review", "publishedPageIds": [], "reviewCount": 1,
                             "error": QUEUE_FULL_ERROR})
        self.assertEqual(result["finalizeErrors"], 0)
        self.assertFalse((self.state / "resolved/batch-full.json").exists())
        self.assertTrue((self.state / "audit/batch-full.json").exists())


if __name__ == "__main__":
    unittest.main()
