"""A full review queue makes new work wait instead of becoming a silent hold.

Given a project whose pending reviews reached ``maxPendingPerProject``, the
worker must keep that project's queued turns untouched, spend no budget and
invoke no model, while still letting the existing guard take over once the
shared intake queue is crowded, so one backlog cannot block every project.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from common import save_json
from queue_worker import process_queue
from review_capacity import should_wait_for_review
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

    def enqueue(self, name):
        job = {"id": name, "projectId": "growth", "sessionId": "session", "prompt": name,
               "evidence": [{"id": name, "kind": "user", "text": name}]}
        save_json(self.state / "queue" / (name + ".json"), job)
        return self.state / "queue" / (name + ".json")

    def drain(self):
        def invoke(*args):
            self.calls.append(args)
            return {"status": "empty", "publishedPageIds": []}
        return process_queue(self.config, invoke, clock=lambda: self.now)

    def test_full_review_queue_defers_without_budget_or_invoke(self):
        """Given a full review queue, When the worker wakes, Then the turn waits untouched."""
        self.hold("old-1")
        self.hold("old-2")
        queued = self.enqueue("turn")
        before = queued.read_bytes()
        result = self.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(result["reason"], "review-queue-full")
        self.assertFalse((self.state / "daily-budget.json").exists())
        self.assertEqual(queued.read_bytes(), before)
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
        result = self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(self.calls[0][2]["job"]["sourceJobIds"]), ["turn", "turn-2"])
        self.assertEqual(result["processed"], 2)
        self.assertEqual(list((self.state / "queue").glob("*.json")), [])

    def test_already_claimed_batch_is_not_held_back(self):
        """A batch claimed before the queue filled continues so its durable result can finalize."""
        self.hold("old-1")
        self.hold("old-2")
        job = self.enqueue("turn").read_text()
        merged = {**json.loads(job), "id": "batch-claimed", "sourceJobIds": ["turn"],
                  "sourceQueueFiles": ["turn.json"]}
        save_json(self.state / "batches/batch-claimed.json", {"version": 1, "batchId": "batch-claimed",
                  "status": "claimed", "queueFiles": ["turn.json"], "job": merged})
        self.drain()
        self.assertEqual(len(self.calls), 1)

    def test_other_projects_reviews_do_not_block(self):
        """Holds of another project never count against this project's capacity."""
        self.hold("other-1", project="elsewhere")
        self.hold("other-2", project="elsewhere")
        self.enqueue("turn")
        self.drain()
        self.assertEqual(len(self.calls), 1)

    def test_crowded_intake_queue_falls_back_to_the_existing_guard(self):
        """Given half the shared queue is used, waiting stops so other projects keep room."""
        self.config["maxQueuedJobs"] = 4
        self.hold("old-1")
        self.hold("old-2")
        self.enqueue("turn-a")
        self.enqueue("turn-b")
        self.drain()
        self.assertEqual(len(self.calls), 1)

    def test_retry_does_not_count_the_hold_it_replaces(self):
        """A reprocessing attempt frees the slot of the review it would replace."""
        self.hold("old-1")
        self.hold("old-2")
        job = {"projectId": "growth", "reviewRetryOf": "old-2"}
        self.assertFalse(should_wait_for_review(self.config, job))
        self.assertTrue(should_wait_for_review(self.config, {"projectId": "growth"}))

    def test_missing_or_invalid_limit_leaves_validation_to_the_worker(self):
        """Without a valid configured limit the pre-check stays out of the way."""
        self.hold("old-1")
        for limit in (None, 0, "2"):
            self.config["maxPendingPerProject"] = limit
            self.assertFalse(should_wait_for_review(self.config, {"projectId": "growth"}))


if __name__ == "__main__":
    unittest.main()
