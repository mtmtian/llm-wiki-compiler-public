"""A durable result that breaks the publication contract becomes a review hold.

Given a frozen batch whose saved model result predates the current evidence-role
contract, finalization can never publish it: the result is reused verbatim on
every attempt. The worker must not back off forever. The batch completes as an
ordinary hold that keeps the rejected result in its audit and can be drafted
again with ``--retry-review``. Other finalization failures keep their backoff.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json, save_json
from publication_hold import CONTRACT_HOLD_ERROR
from queue_worker import process_queue
from replica import initialize_baseline
from review_retry import retry_review


def _evidence(identifier, kind, text):
    return {"id": identifier, "kind": kind, "text": text, "sha256": digest(text),
            "observedAt": "2026-09-18T00:00:00Z", "locator": "session:" + identifier}


def _old_contribution():
    """An assistant quote supporting an assistant-primary lesson, which the contract forbids."""
    primary, support = _evidence("a1", "assistant", "assistant summary"), _evidence("a2", "assistant", "more context")
    claim = {"text": "lesson", "quote": primary["text"], "evidenceId": "a1", "useWhen": "always",
             "title": "lesson", "topic": "t", "slug": "lesson", "targetPageId": None,
             "kind": "lesson", "status": "historical", "rationale": "old draft",
             "supportingQuotes": [{"evidenceId": "a2", "quote": support["text"]}]}
    return {"claims": [claim], "evidence": [primary, support]}


class PublicationHoldTests(unittest.TestCase):
    """Run the real worker and v2 publication path; no model call is allowed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state, self.exchange, shared = root / "state", root / "exchange", root / "shared"
        (shared / "wiki/concepts").mkdir(parents=True)
        (shared / "wiki/concepts/base.md").write_text("# Base", encoding="utf-8")
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "maxDailyJobs": 100, "machineId": "a",
                       "publishEnabled": True, "model": "gpt-test", "stateDir": str(self.state),
                       "wikiRoot": str(self.state / "replica/current"), "sharedWikiRoot": str(shared),
                       "projects": {"project": {"label": "Project", "pages": []}},
                       "exchange": {"protocolVersion": 2, "root": str(self.exchange), "participants": ["a"]}}
        initialize_baseline(self.config)
        self.result = {"status": "submitted", "publishedPageIds": [], "contribution": _old_contribution()}
        self.freeze()

    def freeze(self):
        source = {"id": "turn-old", "projectId": "project", "sessionId": "s",
                  "evidence": self.result["contribution"]["evidence"]}
        save_json(self.state / "queue/turn-old.json", source)
        job = {**source, "id": "batch-old", "projectLabel": "Project", "createdAt": "2026-09-18T00:00:00Z",
               "sourceJobIds": ["turn-old"], "basisRecordIds": [], "allowedPageIds": [],
               "sessionContext": {"version": 1, "revision": 1, "summary": "", "topicPageIds": [], "evidence": []}}
        save_json(self.state / "batches/batch-old.json", {
            "batchId": "batch-old", "status": "finalize-retry", "queueFiles": ["turn-old.json"], "job": job,
            "result": self.result, "finalizeAttempts": 22, "nextFinalizeAt": self.now.isoformat()})

    def drain(self):
        def no_model(*_args):
            raise AssertionError("a frozen result must not call the model")
        return process_queue(self.config, no_model, clock=lambda: self.now)

    def test_contract_violation_completes_as_hold_and_keeps_rejected_result(self):
        """When the frozen result can never publish, Then it is held instead of retried."""
        self.drain()
        audit = load_json(self.state / "batches/batch-old.json")
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["result"]["status"], "needs_review")
        self.assertTrue(audit["result"]["error"].startswith(CONTRACT_HOLD_ERROR))
        self.assertNotIn("contribution", audit["result"])
        self.assertEqual(audit["unpublishedResult"], self.result)
        review = load_json(self.state / "review/batch-old.json")
        self.assertEqual((review["jobId"], review["projectId"]), ("batch-old", "project"))
        self.assertIn("assistant evidence", review["decisions"][0]["reason"])
        self.assertFalse((self.state / "queue/turn-old.json").exists())
        self.assertFalse(list(self.exchange.glob("v2/publications/a/*.json")))

    def test_held_contract_violation_can_be_drafted_again(self):
        """Given the hold, When an operator asks to reprocess it, Then the existing retry accepts it."""
        self.drain()
        staged = retry_review(self.config, "batch-old", dry_run=True, clock=lambda: self.now)
        self.assertEqual(staged["status"], "ready")
        self.assertEqual(staged["reviewJobId"], "batch-old")

    def test_other_finalization_errors_keep_their_backoff(self):
        """Given an export failure that is not a contract violation, Then the batch backs off as before."""
        with patch("queue_worker.export_result", side_effect=ValueError("Conflicting exchange write")):
            self.drain()
        audit = load_json(self.state / "batches/batch-old.json")
        self.assertEqual((audit["status"], audit["finalizeAttempts"]), ("finalize-retry", 23))
        self.assertGreater(audit["nextFinalizeAt"], (self.now + timedelta(seconds=1)).isoformat())
        self.assertFalse((self.state / "review/batch-old.json").exists())


if __name__ == "__main__":
    unittest.main()
