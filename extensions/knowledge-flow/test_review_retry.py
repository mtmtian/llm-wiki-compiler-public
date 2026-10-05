"""Review reprocessing preserves evidence and cannot turn a hold into approval.

Given a completed, held session batch, an explicit retry gets a new model
attempt against the current replica. Original evidence and results stay frozen;
only a normal terminal result may retire the old notification.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import queue_worker

from common import load_json, save_json
from queue_worker import process_queue
from review_retry import retry_review
from replica_integrity import seal_generation


class ReviewRetryTests(unittest.TestCase):
    """Exercise real queue finalization with an external model boundary fake."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "projects": {"p": {"pages": []}}, "maxDailyJobs": 100}
        self.review = {"jobId": "batch-old", "projectId": "p", "claims": [],
                       "decisions": [{"decision": "needs_review", "reason": "bad quote"}],
                       "sessionReview": {"inputEvidence": []}}
        self.job = {"id": "batch-old", "projectId": "p", "sessionId": "session",
                    "sourceJobIds": ["turn-old"], "evidence": [{"id": "e", "text": "original"}],
                    "sessionContext": {"version": 1, "revision": 2, "summary": "old context",
                                       "topicPageIds": [], "evidence": []}, "allowedPageIds": []}
        save_json(self.state / "review/batch-old.json", self.review)
        save_json(self.state / "batches/batch-old.json", {"batchId": "batch-old", "status": "completed",
                  "job": self.job, "result": {"status": "needs_review"}, "basisRecordIds": ["original-basis"]})
        save_json(self.state / "completed/turn-old.json", {"status": "needs_review"})

    def retry(self, dry_run=False):
        return retry_review(self.config, "batch-old", dry_run=dry_run, clock=lambda: self.now)

    def test_dry_run_keeps_original_review_and_does_not_enqueue(self):
        """Given a held batch, dry-run describes a retry without any state write."""
        before = sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*.json"))
        result = self.retry(dry_run=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(before, sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*.json")))
        self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)

    def test_retry_is_idempotent_and_keeps_original_frozen_batch(self):
        """Repeated requests enqueue one independent attempt with exact old evidence."""
        frozen = (self.state / "batches/batch-old.json").read_bytes()
        first, second = self.retry(), self.retry()
        self.assertEqual(first["retryJobId"], second["retryJobId"])
        self.assertEqual(len(list((self.state / "queue").glob("*.json"))), 1)
        queued = load_json(next((self.state / "queue").glob("*.json")))
        self.assertEqual(queued["evidence"], self.job["evidence"])
        self.assertEqual(queued["sessionContext"], self.job["sessionContext"])
        self.assertNotIn("basisRecordIds", queued)
        self.assertEqual((self.state / "batches/batch-old.json").read_bytes(), frozen)

    def test_hold_from_before_semantic_topics_is_retried_in_todays_scope(self):
        """Given a legacy hold and active semantic topics, the retry is a semantic job like new intake."""
        self.config.update(topicScope="semantic", exchange={"protocolVersion": 2})
        self.retry()
        queued = load_json(next((self.state / "queue").glob("*.json")))
        self.assertEqual(queued["topicScope"], "semantic")
        self.assertEqual(queued["evidence"], self.job["evidence"])

    def test_retry_drops_a_scope_that_is_no_longer_active(self):
        """Given a semantic hold after semantic topics are off, the retry takes today's project scope."""
        batch = load_json(self.state / "batches/batch-old.json")
        batch["job"]["topicScope"] = "semantic"
        save_json(self.state / "batches/batch-old.json", batch)
        self.retry()
        self.assertNotIn("topicScope", load_json(next((self.state / "queue").glob("*.json"))))

    def test_success_retires_review_after_finalization_without_regressing_session(self):
        """A normally reviewed empty result clears the hold without recommitting old turns."""
        queued = self.retry()
        seen = []
        def invoke(_config, _op, payload, _timeout):
            seen.append(payload["job"])
            return {"status": "empty", "publishedPageIds": [], "sessionMemory": {"summary": "done"}}
        with patch("queue_finalization.commit_batch", side_effect=AssertionError("old session cannot regress")):
            result = process_queue(self.config, invoke, clock=lambda: self.now)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(seen[0]["id"], queued["retryJobId"])
        self.assertEqual(seen[0]["sessionContext"], self.job["sessionContext"])
        self.assertFalse((self.state / "review/batch-old.json").exists())
        self.assertEqual(load_json(self.state / "completed/turn-old.json")["status"], "needs_review")
        self.assertEqual(load_json(self.state / "resolved/batch-old.json")["action"], "reprocessed")
        self.assertFalse(list((self.state / "session-state").glob("*.json")))

    def test_still_uncertain_replaces_hold_with_new_review_without_approval(self):
        """Independent review still holding creates one live successor, not a hidden loss."""
        queued = self.retry()
        new_id = queued["retryJobId"]
        def invoke(*_args):
            save_json(self.state / "review" / (new_id + ".json"), {"jobId": new_id, "projectId": "p"})
            return {"status": "needs_review", "reviewFile": str(self.state / "review" / (new_id + ".json"))}
        process_queue(self.config, invoke, clock=lambda: self.now)
        self.assertEqual([p.stem for p in (self.state / "review").glob("*.json")], [new_id])
        self.assertEqual(load_json(self.state / "resolved/batch-old.json")["result"]["status"], "needs_review")

    def test_failure_and_missing_successor_never_remove_original(self):
        """Failed processing or a lost successor must leave the original actionable."""
        self.retry()
        process_queue(self.config, lambda *_: {"status": "needs_review"}, clock=lambda: self.now)
        self.assertTrue((self.state / "review/batch-old.json").exists())
        self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)

    def test_changed_review_preserves_both_and_reports_drift(self):
        """Concurrent manual review changes are never silently retired by an older attempt."""
        self.retry()
        save_json(self.state / "review/batch-old.json", {**self.review, "operatorNote": "changed"})
        submitted = []
        result = process_queue(self.config, lambda *args: submitted.append(args) or {"status": "empty"}, clock=lambda: self.now)
        self.assertTrue((self.state / "review/batch-old.json").exists())
        self.assertEqual(submitted, [])
        self.assertEqual(result["processed"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json", {}).get("used", 0), 0)

    def test_mutated_retry_request_cannot_replace_the_original_review(self):
        """Given frozen input, changing a queued field quarantines it before model work."""
        retry = self.retry()
        queued = self.state / "queue" / (retry["retryJobId"] + ".json")
        original = load_json(queued)
        changes = ({"projectId": "q"}, {"evidence": [{"id": "e", "text": "different"}]},
                   {"reviewRetryOf": None}, {"id": "different"}, {"sessionContext": {}})
        for change in changes:
            with self.subTest(change=change):
                save_json(queued, {**original, **change})
                result = process_queue(self.config, lambda *_: self.fail("changed input invoked model"),
                                       clock=lambda: self.now)
                self.assertFalse(queued.exists())
                self.assertEqual(result["processed"], 0)
                self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)
                self.assertEqual(load_json(self.state / "daily-budget.json", {}).get("used", 0), 0)

    def test_changed_durable_retry_cannot_consume_budget_or_publish(self):
        """Given a model failure, changing its frozen audit blocks the next attempt."""
        retry = self.retry()
        with patch("queue_finalization.export_result", side_effect=AssertionError("changed audit exported")):
            process_queue(self.config, lambda *_: {"status": "error"}, clock=lambda: self.now)
            audit_path = self.state / "batches" / (retry["retryJobId"] + ".json")
            audit = load_json(audit_path)
            audit["job"]["evidence"][0]["text"] = "changed after first failure"
            save_json(audit_path, audit)
            later = self.now + timedelta(hours=1)
            result = process_queue(self.config, lambda *_: self.fail("changed audit invoked model"),
                                   clock=lambda: later)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 1)
        self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)

    def test_explicit_retry_after_terminal_failure_gets_a_fresh_attempt(self):
        """After three failures, an operator can retry without rewriting the failed evidence."""
        first = self.retry()
        for hour in range(3):
            process_queue(self.config, lambda *_: {"status": "error"},
                          clock=lambda: self.now + timedelta(hours=hour))
        failed = self.state / "failed" / (first["retryJobId"] + ".json")
        frozen = failed.read_bytes()
        second = self.retry()
        self.assertEqual(second["status"], "queued")
        self.assertNotEqual(first["retryJobId"], second["retryJobId"])
        self.assertEqual(self.retry()["retryJobId"], second["retryJobId"])
        process_queue(self.config, lambda *_: {"status": "empty"}, clock=lambda: self.now)
        self.assertEqual(failed.read_bytes(), frozen)
        self.assertFalse((self.state / "review/batch-old.json").exists())

    def test_prepared_scope_cannot_change_between_attempts(self):
        """Given a failed attempt, page destinations and publication basis remain frozen."""
        retry = self.retry()
        process_queue(self.config, lambda *_: {"status": "error"}, clock=lambda: self.now)
        audit_path = self.state / "batches" / (retry["retryJobId"] + ".json")
        audit = load_json(audit_path)
        audit["job"].update(allowedPageIds=["concepts/foreign-page"], basisRecordIds=["forged"])
        save_json(audit_path, audit)
        seen = []
        result = process_queue(self.config, lambda *args: seen.append(args) or {"status": "empty"},
                               clock=lambda: self.now + timedelta(hours=1))
        self.assertEqual(seen, [])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 1)
        self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)

    def test_source_drift_during_preparation_does_not_spend_budget(self):
        """When source changes while replica preparation runs, no model budget is spent."""
        self.retry()
        prepare = queue_worker._prepare_batch
        changed = {**self.review, "operatorNote": "changed during sync"}
        def prepare_then_change(*args):
            result = prepare(*args)
            save_json(self.state / "review/batch-old.json", changed)
            return result
        seen = []
        with patch("queue_worker._prepare_batch", side_effect=prepare_then_change):
            result = process_queue(self.config, lambda *args: seen.append(args) or {"status": "empty"},
                                   clock=lambda: self.now)
        self.assertEqual(seen, [])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json", {}).get("used", 0), 0)
        self.assertEqual(load_json(self.state / "review/batch-old.json"), changed)

    def test_pinned_v2_generation_is_reused_through_model_failure(self):
        """Given a sealed replica, a retry uses its exact scope on both model attempts."""
        retry = self.retry()
        generation = self.state / "replica/generations" / ("a" * 64)
        save_json(generation / ".llmwiki/replica-response.json", {"pages": 0, "conflicts": []})
        seal_generation(generation, generation.name)
        self.config.update(machineId="a", publishEnabled=False, wikiRoot=str(generation),
                           sharedWikiRoot=str(self.state / "shared"),
                           exchange={"protocolVersion": 2, "root": str(self.state / "exchange"),
                                     "legacyImporterMachineId": "a", "participants": ["a"]})
        queued = load_json(self.state / "queue" / (retry["retryJobId"] + ".json"))
        queued["basisRecordIds"] = []
        audit = {"batchId": queued["id"], "status": "claimed", "job": queued,
                 "queueFiles": [queued["id"] + ".json"], "replicaBasis": {
                     "generation": generation.name, "wikiRoot": str(generation), "basisRecordIds": []}}
        save_json(self.state / "batches" / (queued["id"] + ".json"), audit)
        seen = []
        def invoke(config, _operation, payload, _timeout):
            seen.append((config["wikiRoot"], payload["job"]["basisRecordIds"]))
            return {"status": "error" if len(seen) == 1 else "empty"}
        process_queue(self.config, invoke, clock=lambda: self.now)
        process_queue(self.config, invoke, clock=lambda: self.now + timedelta(hours=1))
        self.assertEqual(seen, [(str(generation), []), (str(generation), [])])
        self.assertFalse((self.state / "review/batch-old.json").exists())

    def test_scope_drift_after_model_cannot_retire_or_export(self):
        """When a prepared job changes before finalization, its original hold survives."""
        self.retry()
        def invoke(_config, _operation, payload, _timeout):
            payload["job"]["allowedPageIds"] = ["concepts/foreign-page"]
            return {"status": "empty"}
        with patch("queue_finalization.export_result", side_effect=AssertionError("changed scope exported")):
            result = process_queue(self.config, invoke, clock=lambda: self.now)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(load_json(self.state / "review/batch-old.json"), self.review)
        self.assertFalse((self.state / "resolved/batch-old.json").exists())

    def test_unsafe_id_published_batch_and_capacity_are_rejected(self):
        """Retry is not a general replay/publication endpoint or a queue-limit bypass."""
        with self.assertRaises(ValueError):
            retry_review(self.config, "../batch-old")
        audit = load_json(self.state / "batches/batch-old.json")
        save_json(self.state / "batches/batch-old.json", {**audit, "result": {"status": "submitted"}})
        with self.assertRaises(ValueError):
            self.retry()
        save_json(self.state / "batches/batch-old.json", audit)
        with self.assertRaises(ValueError):
            retry_review({**self.config, "maxQueuedJobs": 0}, "batch-old")


if __name__ == "__main__":
    unittest.main()
