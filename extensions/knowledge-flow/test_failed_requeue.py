"""A failed turn can be requeued once, as a new job, through the ordinary intake path.

Given a turn that intake refused as too large (its stale session context made it so) or that the worker
gave up on, an explicit requeue copies its evidence into a new job, attaches today's session context,
applies the byte limit, and archives the failed source with its capture error. A turn that is still too
large stays failed, untouched.
"""
import fcntl
import tempfile
import unittest
from pathlib import Path

from common import load_json, save_json
from failed_requeue import requeue_failed

DECISION = "决定以后按字节计量样例窗口，并保留原始证据。"
STALE_CONTEXT = {"version": 1, "revision": 9, "summary": "旧摘要", "topicPageIds": [],
                 "evidence": [{"id": "old", "kind": "assistant", "text": "旧回复" * 30_000}]}


class FailedRequeueTests(unittest.TestCase):
    """Given/When/Then checks against the real intake, queue and session state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "projects": {"growth": {"pages": []}}, "maxJobBytes": 120_000,
                       "sessionConsolidation": {"enabled": True}, "eventDriven": {"enabled": True}}

    def failed(self, name, text=DECISION, **extra):
        """A failed turn source as intake or the worker leaves it."""
        source = {"id": name, "projectId": "growth", "sessionId": "session-1", "prompt": text,
                  "createdAt": "2026-10-03T00:00:00Z", "captureStatus": "ok",
                  "evidence": [{"id": name, "kind": "user", "text": text}], **extra}
        save_json(self.state / "failed" / f"{name}.json", source)
        return source

    def too_large(self, name):
        """Refused at intake: the stale session context pushed the job over the byte limit."""
        error = {"at": "2026-10-03T00:00:01Z", "type": "JobTooLarge", "status": "error", "jobId": name,
                 "jobBytes": 180_000, "maxJobBytes": 120_000}
        save_json(self.state / "capture-errors" / f"{name}.json", error)
        return self.failed(name, sessionContext=STALE_CONTEXT, **error)

    def snapshot(self):
        return sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*.json"))

    def test_dry_run_reports_readiness_without_writing(self):
        """Given a turn refused as too large, When dry-run, Then it is ready at today's size and nothing changes."""
        self.too_large("turn-a")
        before = self.snapshot()
        result = requeue_failed(self.config, "turn-a", dry_run=True)
        self.assertEqual(result["status"], "ready")
        self.assertLess(result["jobBytes"], result["maxJobBytes"])
        self.assertEqual(self.snapshot(), before)

    def test_requeue_sends_the_turn_through_intake_and_archives_the_failure(self):
        """Given a turn refused as too large, When requeued, Then a new job is queued with today's context."""
        source = self.too_large("turn-a")
        result = requeue_failed(self.config, "turn-a")
        self.assertEqual((result["status"], result["requeueJobId"]), ("queued", "turn-a-requeue"))
        queued = load_json(self.state / "queue" / "turn-a-requeue.json")
        self.assertEqual((queued["requeueOf"], queued["evidence"]), ("turn-a", source["evidence"]))
        self.assertNotEqual(queued["sessionContext"], STALE_CONTEXT)
        self.assertFalse({"type", "jobBytes", "attempts", "nextAttemptAt"} & set(queued))
        self.assertIn("sessionSchedule", queued)
        resolved = load_json(self.state / "resolved" / "turn-a.json")
        self.assertEqual((resolved["action"], resolved["requeueJobId"]), ("requeued", "turn-a-requeue"))
        self.assertEqual((resolved["source"], resolved["captureError"]["type"]), (source, "JobTooLarge"))
        self.assertFalse((self.state / "failed" / "turn-a.json").exists())
        self.assertFalse((self.state / "capture-errors" / "turn-a.json").exists())
        with self.assertRaisesRegex(ValueError, "failed turn source"):
            requeue_failed(self.config, "turn-a")

    def test_turn_the_worker_gave_up_on_is_requeued_under_a_fresh_identity(self):
        """Given a turn failed after three worker attempts, When requeued, Then its failed batch is left alone."""
        self.failed("turn-b", attempts=3, nextAttemptAt="2026-10-03T01:00:00Z", queueFile="turn-b.json")
        save_json(self.state / "batches" / "batch-old.json", {"status": "failed", "queueFiles": ["turn-b.json"]})
        save_json(self.state / "completed" / "turn-b-requeue.json", {"status": "empty"})
        result = requeue_failed(self.config, "turn-b")
        self.assertEqual((result["status"], result["requeueJobId"]), ("queued", "turn-b-requeue-2"))
        self.assertEqual(load_json(self.state / "batches" / "batch-old.json")["status"], "failed")
        self.assertEqual(load_json(self.state / "queue" / "turn-b-requeue-2.json")["requeueOf"], "turn-b")

    def test_turn_still_too_large_stays_failed(self):
        """Given a turn whose own evidence exceeds the limit, When requeued, Then nothing changes."""
        self.failed("turn-c", text="很长的决定" * 30_000)
        before = self.snapshot()
        self.assertEqual(requeue_failed(self.config, "turn-c")["status"], "too-large")
        self.assertEqual(self.snapshot(), before)

    def test_turn_without_substance_completes_empty_and_is_archived(self):
        """Given a bare acknowledgement, When requeued, Then intake completes it as empty and the failure resolves."""
        self.failed("turn-d", text="好的")
        self.assertEqual(requeue_failed(self.config, "turn-d")["status"], "empty")
        self.assertEqual(load_json(self.state / "resolved" / "turn-d.json")["outcome"], "empty")

    def test_review_full_requeue_is_archived_as_a_recoverable_capacity_wait(self):
        """A review-capacity wait is durable and must not strand the failed source."""
        self.failed("turn-review-full")
        self.config["maxPendingPerProject"] = 1
        save_json(self.state / "review/held.json", {"jobId": "held", "projectId": "growth"})

        result = requeue_failed(self.config, "turn-review-full")

        self.assertEqual(result["status"], "deferred")
        pending = load_json(self.state / "capture-pending/turn-review-full-requeue.json")
        self.assertEqual(pending["kind"], "capacity")
        self.assertEqual(pending["job"]["requeueOf"], "turn-review-full")
        archive = load_json(self.state / "resolved/turn-review-full.json")
        self.assertEqual((archive["action"], archive["outcome"]), ("requeued", "deferred"))
        self.assertFalse((self.state / "failed/turn-review-full.json").exists())

    def test_busy_worker_and_invalid_sources_are_refused(self):
        """Given a running worker, an unknown name or a review retry, Then requeue refuses without writing."""
        self.failed("turn-e")
        self.failed("turn-f", reviewRetryOf="batch-old")
        with (self.state / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(requeue_failed(self.config, "turn-e")["status"], "busy")
        for name in ("missing", "turn-f", "../turn-e"):
            with self.subTest(name), self.assertRaises(ValueError):
                requeue_failed(self.config, name)
        self.assertTrue((self.state / "failed" / "turn-e.json").exists())


if __name__ == "__main__":
    unittest.main()
