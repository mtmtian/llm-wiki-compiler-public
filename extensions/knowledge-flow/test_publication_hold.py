"""A durable result that breaks the publication contract becomes a terminal failure.

Given a frozen batch whose saved model result predates the current evidence-role
contract, finalization can never publish it: the result is reused verbatim on
every attempt. The worker must not back off forever or turn the technical
failure into human review. The audit keeps the rejected result and the source
moves to failed. Other finalization failures keep their backoff.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json, save_json
from queue_worker import process_queue
from replica import initialize_baseline


def _evidence(identifier, kind, text):
    return {"id": identifier, "kind": kind, "text": text, "sha256": digest(text),
            "observedAt": "2026-09-18T00:00:00Z", "locator": "session:" + identifier}


def _claim(evidence, kind, status, **extra):
    return {"text": kind, "quote": evidence["text"], "evidenceId": evidence["id"], "useWhen": "always",
            "title": kind, "topic": "t", "slug": kind, "targetPageId": None,
            "kind": kind, "status": status, "rationale": "old draft", **extra}


def _old_contribution():
    """An assistant quote supporting an assistant-primary lesson, which the contract forbids."""
    primary, support = _evidence("a1", "assistant", "assistant summary"), _evidence("a2", "assistant", "more context")
    claim = _claim(primary, "lesson", "historical",
                   supportingQuotes=[{"evidenceId": "a2", "quote": support["text"]}])
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
        self.freeze(_old_contribution())

    def freeze(self, contribution):
        self.result = {"status": "submitted", "publishedPageIds": [], "contribution": contribution}
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

    def test_contract_violation_is_terminal_and_keeps_rejected_result(self):
        """When the frozen result can never publish, Then it fails without creating review."""
        self.drain()
        audit = load_json(self.state / "batches/batch-old.json")
        self.assertEqual(audit["status"], "failed")
        self.assertEqual((audit["result"]["status"], audit["result"]["retryable"]), ("error", False))
        self.assertIn("publication contract failure", audit["result"]["error"])
        self.assertEqual(audit["unpublishedResult"], self.result)
        self.assertFalse((self.state / "queue/turn-old.json").exists())
        self.assertTrue((self.state / "failed/turn-old.json").exists())
        self.assertFalse((self.state / "review/batch-old.json").exists())
        self.assertFalse(list(self.exchange.glob("v2/publications/a/*.json")))

    def test_permanent_contract_failure_cannot_be_retried_as_a_review(self):
        """A technical failure does not create a manual review retry path."""
        self.drain()
        from review_retry import retry_review
        with self.assertRaisesRegex(ValueError, "completed held session"):
            retry_review(self.config, "batch-old", dry_run=True, clock=lambda: self.now)

    def test_violation_found_only_by_packet_validation_is_terminal_too(self):
        """Packet validation failures use the same technical-failure lifecycle."""
        user, unused = _evidence("u", "user", "keep the launcher"), _evidence("x", "user", "unused remark")
        self.freeze({"claims": [_claim(user, "decision", "decided")], "evidence": [user, unused]})
        self.drain()
        audit = load_json(self.state / "batches/batch-old.json")
        self.assertEqual((audit["status"], audit["result"]["status"]), ("failed", "error"))
        self.assertIn("publication contract failure", audit["result"]["error"])
        self.assertIn("not referenced by a claim", audit["result"]["error"])
        self.assertFalse((self.state / "review/batch-old.json").exists())

    def test_other_finalization_errors_keep_their_backoff(self):
        """Given an export failure that is not a contract violation, Then the batch backs off as before."""
        with patch("queue_finalization.export_result", side_effect=ValueError("Conflicting exchange write")):
            self.drain()
        audit = load_json(self.state / "batches/batch-old.json")
        self.assertEqual((audit["status"], audit["finalizeAttempts"]), ("finalize-retry", 23))
        self.assertGreater(audit["nextFinalizeAt"], (self.now + timedelta(seconds=1)).isoformat())
        self.assertFalse((self.state / "review/batch-old.json").exists())


if __name__ == "__main__":
    unittest.main()
