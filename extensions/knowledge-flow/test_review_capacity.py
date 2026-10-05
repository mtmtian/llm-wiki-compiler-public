"""A full review queue parks work as capacity waits while other projects run."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import load_json, save_json
from capture_retry import process_capture_retries
from queue_worker import process_queue
from review_capacity import review_queue_full
from wake import _reasons


class ReviewCapacityTests(unittest.TestCase):
    """Drive the real queue worker with an external model boundary fake."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "projects": {"growth": {"pages": []}}, "maxDailyJobs": 100,
                       "maxPendingPerProject": 2, "maxQueuedJobs": 30}
        self.calls = []

    def hold(self, name, project="growth"):
        save_json(self.state / "review" / (name + ".json"), {"jobId": name, "projectId": project})

    def enqueue(self, name, project="growth"):
        job = {"id": name, "projectId": project, "sessionId": "session", "prompt": name,
               "evidence": [{"id": name, "kind": "user", "text": name}]}
        save_json(self.state / "queue" / (name + ".json"), job)
        return self.state / "queue" / (name + ".json")

    def drain(self, now=None):
        def invoke(*args):
            self.calls.append(args)
            return {"status": "empty", "publishedPageIds": []}
        now = now or self.now
        process_capture_retries(self.config, now)
        return process_queue(self.config, invoke, clock=lambda: now)

    def test_full_review_queue_parks_complete_input_without_budget_or_invoke(self):
        """A full project moves its unclaimed source to durable capacity wait."""
        self.hold("old-1")
        self.hold("old-2")
        queued = self.enqueue("turn")
        before = queued.read_bytes()
        result = self.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(result["reason"], "review-queue-full")
        self.assertFalse((self.state / "daily-budget.json").exists())
        self.assertFalse(queued.exists())
        self.assertTrue((self.state / "capture-pending/turn.json").is_file())
        self.assertEqual(json.loads(before)["evidence"],
                         load_json(self.state / "capture-pending/turn.json")["job"]["evidence"])
        self.assertEqual(list((self.state / "batches").glob("*.json")), [])
        self.assertIn("review-queue-full", _reasons({}, {}, result))

    def test_freed_review_slot_runs_waiting_turns_as_one_batch(self):
        """Given turns that waited unclaimed, When one review is resolved, Then they run together."""
        self.hold("old-1")
        self.hold("old-2")
        self.enqueue("turn")
        self.drain()
        self.enqueue("turn-2")
        self.drain()
        (self.state / "review/old-2.json").unlink()
        process_capture_retries(self.config, self.now + timedelta(seconds=301))
        result = self.drain(self.now + timedelta(seconds=602))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(self.calls[0][2]["job"]["sourceJobIds"]), ["turn", "turn-2"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(list((self.state / "queue").glob("*.json")), [])

    def test_claimed_batch_stays_in_queue_when_capacity_becomes_full(self):
        """A claimed source remains recoverable when review capacity closes before invocation."""
        self.hold("old-1")
        self.hold("old-2")
        job = self.enqueue("turn").read_text()
        merged = {**json.loads(job), "id": "batch-claimed", "sourceJobIds": ["turn"],
                  "sourceQueueFiles": ["turn.json"]}
        save_json(self.state / "batches/batch-claimed.json", {"version": 1, "batchId": "batch-claimed",
                  "status": "claimed", "queueFiles": ["turn.json"], "job": merged})
        self.drain()
        self.assertEqual(self.calls, [])
        self.assertTrue((self.state / "queue/turn.json").exists())
        self.assertEqual(load_json(self.state / "batches/batch-claimed.json")["status"],
                         "capacity-deferred")

    def test_other_projects_reviews_do_not_block(self):
        """Holds of another project never count against this project's capacity."""
        self.hold("other-1", project="elsewhere")
        self.hold("other-2", project="elsewhere")
        self.enqueue("turn")
        self.drain()
        self.assertEqual(len(self.calls), 1)

    def test_full_project_parking_keeps_unrelated_project_runnable(self):
        """Parked work from one full project leaves a runnable slot for another project."""
        self.config["maxQueuedJobs"] = 4
        self.config["projects"]["other"] = {"pages": []}
        self.hold("old-1")
        self.hold("old-2")
        self.enqueue("turn-a")
        self.enqueue("turn-b")
        self.enqueue("turn-c", project="other")
        self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2]["job"]["projectId"], "other")

    def test_retry_does_not_count_the_hold_it_replaces(self):
        """A reprocessing attempt frees the slot of the review it would replace."""
        self.hold("old-1")
        self.hold("old-2")
        job = {"projectId": "growth", "reviewRetryOf": "old-2"}
        self.assertFalse(review_queue_full(self.config, job))
        self.assertTrue(review_queue_full(self.config, {"projectId": "growth"}))

    def test_missing_or_invalid_limit_leaves_validation_to_the_worker(self):
        """Without a valid configured limit the pre-check stays out of the way."""
        self.hold("old-1")
        for limit in (None, 0, "2"):
            self.config["maxPendingPerProject"] = limit
            self.assertFalse(review_queue_full(self.config, {"projectId": "growth"}))


if __name__ == "__main__":
    unittest.main()
