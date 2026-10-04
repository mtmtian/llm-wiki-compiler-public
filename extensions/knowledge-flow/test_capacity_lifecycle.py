"""Capacity waits preserve complete jobs and keep technical failures terminal."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admission_usage import admission_usage
from common import load_json, save_json
from capture_retry import process_capture_retries
from hooks import enqueue_job
from queue_worker import process_queue


UTC = dt.timezone.utc


class CapacityLifecycleTests(unittest.TestCase):
    """Use temporary state and a fake only at the external worker boundary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.now = dt.datetime(2026, 10, 5, tzinfo=UTC)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "maxQueuedJobs": 2, "maxPendingPerProject": 1, "maxDailyJobs": 100,
                       "captureRetry": {"baseSeconds": 2, "maxBackoffSeconds": 8},
                       "projects": {"p": {"pages": []}, "q": {"pages": []}},
                       "eventDriven": {"enabled": True}}

    def job(self, identifier, project="p", **extra):
        created = self.now - dt.timedelta(seconds=300)
        value = {"id": identifier, "projectId": project, "sessionId": "s-" + identifier,
                 "createdAt": created.isoformat(), "prompt": "Keep the evidence exactly",
                 "evidence": [{"id": "e-" + identifier, "kind": "user", "sha256": "abc",
                               "text": "frozen source"}]}
        value.update(extra)
        return value

    def test_stop_capacity_rejection_stores_full_job_and_restores_without_transcript(self):
        """A full runtime queue waits on the complete snapshot and later restores that snapshot."""
        save_json(self.state / "queue/blocker.json", self.job("blocker", project="q"))
        self.config["maxQueuedJobs"] = 1
        source = self.job("source")
        enqueue_job(source, self.state / "queue/source.json", self.config)

        pending_path = self.state / "capture-pending/source.json"
        pending = load_json(pending_path)
        self.assertEqual(pending["kind"], "capacity")
        self.assertEqual(pending["job"]["evidence"], source["evidence"])
        self.assertNotIn("event", pending)
        self.assertNotIn("deadlineAt", pending)
        self.assertFalse((self.state / "review/source.json").exists())

        (self.state / "queue/blocker.json").unlink()
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        report = process_capture_retries(self.config, due)

        self.assertEqual(report["recovered"], 1)
        queued = load_json(self.state / "queue/source.json")
        self.assertEqual(queued["evidence"], source["evidence"])
        self.assertEqual(queued["id"], "source")
        self.assertFalse(pending_path.exists())

    def test_capacity_wait_area_full_saves_the_rejected_job_as_admission_failure(self):
        """A full wait area leaves the complete new input visible in failed/."""
        self.config["maxQueuedJobs"] = 1
        save_json(self.state / "queue/blocker.json", self.job("blocker", project="q"))
        enqueue_job(self.job("waiting"), self.state / "queue/waiting.json", self.config)
        save_json(self.state / "queue/second-blocker.json", self.job("second", project="q"))
        enqueue_job(self.job("rejected"), self.state / "queue/rejected.json", self.config)

        failed = load_json(self.state / "failed/rejected.json")
        self.assertEqual(failed["id"], "rejected")
        self.assertEqual(failed["status"], "capacity-admission-failed")
        self.assertIn("admission", failed["error"])
        self.assertEqual(failed["evidence"], self.job("rejected")["evidence"])
        self.assertFalse((self.state / "review/rejected.json").exists())

    def test_review_blocked_unclaimed_source_is_parked_while_other_project_runs(self):
        """Parking releases a queue slot while an unrelated project still processes."""
        save_json(self.state / "review/existing.json", {"jobId": "existing", "projectId": "p"})
        save_json(self.state / "queue/blocked.json", self.job("blocked"))
        save_json(self.state / "queue/other.json", self.job("other", project="q"))
        calls = []

        result = process_queue(self.config,
            lambda _config, _command, payload, _timeout: calls.append(payload["job"]["projectId"])
            or {"status": "empty", "publishedPageIds": []}, clock=lambda: self.now)

        self.assertEqual(calls, ["q"])
        self.assertEqual(result["processed"], 1)
        self.assertFalse((self.state / "queue/blocked.json").exists())
        self.assertEqual(load_json(self.state / "capture-pending/blocked.json")["kind"], "capacity")
        self.assertFalse((self.state / "review/blocked.json").exists())

    def test_late_deferred_result_keeps_claimed_source_recoverable(self):
        """A late capacity result is durable, backs off, and never writes completed."""
        source = self.job("late")
        save_json(self.state / "queue/late.json", source)
        calls = []

        def invoke(*_args):
            save_json(self.state / "review/p-held.json", {"jobId": "p-held", "projectId": "p"})
            calls.append(True)
            return {"status": "deferred", "error": "review queue is full", "reviewCount": 0}

        first = process_queue(self.config, invoke, clock=lambda: self.now)
        second = process_queue(self.config, invoke, clock=lambda: self.now)
        audit = load_json(next((self.state / "batches").glob("*.json")))

        self.assertEqual(first["results"][0]["status"], "deferred")
        self.assertTrue(first["results"][0]["attempted"])
        self.assertEqual(second["attempts"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(audit["status"], "capacity-deferred")
        self.assertIn("queue/late.json", [f"queue/{name}" for name in audit["queueFiles"]])
        self.assertEqual(audit["deferredResult"]["reviewCount"], 0)
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 1)
        self.assertTrue((self.state / "queue/late.json").exists())
        self.assertFalse((self.state / "completed/late.json").exists())
        self.assertFalse((self.state / "review/late.json").exists())

    def test_claimed_deferred_source_does_not_block_ready_project_capacity_wait(self):
        """A claimed refusal remains in place while another project's wait is runnable."""
        self.config["maxQueuedJobs"] = 1
        source = self.job("claimed")
        save_json(self.state / "queue/claimed.json", source)
        enqueue_job(self.job("other", project="q"), self.state / "queue/other.json", self.config)
        pending = load_json(self.state / "capture-pending/other.json")
        calls = []

        def refuse_after_claim(*_args):
            save_json(self.state / "review/p-held.json", {"jobId": "p-held", "projectId": "p"})
            calls.append("p")
            return {"status": "deferred", "error": "review queue is full", "reviewCount": 0}

        process_queue(self.config, refuse_after_claim, clock=lambda: self.now)

        restored = process_capture_retries(
            self.config, dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1))
        resumed = process_queue(self.config,
            lambda _config, _command, payload, _timeout: calls.append(payload["job"]["projectId"])
            or {"status": "empty", "publishedPageIds": []},
            clock=lambda: self.now + dt.timedelta(seconds=400))

        self.assertEqual(restored["recovered"], 1)
        self.assertEqual(calls, ["p", "q"])
        self.assertEqual(resumed["processed"], 1)
        self.assertFalse((self.state / "capture-pending/other.json").exists())
        self.assertTrue((self.state / "queue/claimed.json").exists())
        self.assertFalse((self.state / "queue/other.json").exists())

    def test_unverified_deferred_audit_does_not_release_a_queue_slot(self):
        """A legacy or damaged claim without matching source hashes stays counted runnable."""
        self.config["maxQueuedJobs"] = 1
        source = self.job("old-claim")
        save_json(self.state / "queue/old-claim.json", source)
        save_json(self.state / "review/p-held.json", {"jobId": "p-held", "projectId": "p"})
        merged = {**source, "id": "batch-old-claim", "sourceJobIds": ["old-claim"],
                  "sourceQueueFiles": ["old-claim.json"]}
        save_json(self.state / "batches/batch-old-claim.json", {
            "batchId": "batch-old-claim", "status": "capacity-deferred",
            "queueFiles": ["old-claim.json"], "job": merged})
        enqueue_job(self.job("other", project="q"), self.state / "queue/other.json", self.config)
        pending = load_json(self.state / "capture-pending/other.json")

        restored = process_capture_retries(
            self.config, dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1))

        self.assertEqual(admission_usage(self.state)["runnable"], 1)
        self.assertEqual(restored["recovered"], 0)
        self.assertTrue((self.state / "capture-pending/other.json").exists())
        self.assertFalse((self.state / "queue/other.json").exists())

    def test_deferred_activation_waits_until_runnable_slot_is_free(self):
        """A ready claimed batch cannot reactivate over an already full runnable cap."""
        self.config["maxQueuedJobs"] = 1
        source = self.job("claimed")
        save_json(self.state / "queue/claimed.json", source)
        enqueue_job(self.job("other", project="q"), self.state / "queue/other.json", self.config)
        pending = load_json(self.state / "capture-pending/other.json")

        def refuse_after_claim(*_args):
            save_json(self.state / "review/p-held.json", {"jobId": "p-held", "projectId": "p"})
            return {"status": "deferred", "error": "review queue is full", "reviewCount": 0}

        process_queue(self.config, refuse_after_claim, clock=lambda: self.now)
        process_capture_retries(
            self.config, dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1))
        self.assertTrue((self.state / "queue/other.json").exists())
        (self.state / "review/p-held.json").unlink()

        first = process_queue(self.config,
            lambda _config, _command, payload, _timeout: {"status": "empty", "publishedPageIds": []},
            limit=1, clock=lambda: self.now + dt.timedelta(seconds=400))
        claimed_audit = next(load_json(path) for path in (self.state / "batches").glob("*.json")
                             if load_json(path).get("queueFiles") == ["claimed.json"])

        self.assertEqual(first["processed"], 1)
        self.assertEqual(claimed_audit["status"], "capacity-deferred")
        self.assertEqual(admission_usage(self.state)["runnable"], 0)

    def test_interrupted_pending_to_queue_transfer_is_counted_once_and_reconciles(self):
        """A crash after queue persistence keeps one admission and retry clears the duplicate."""
        self.config["maxQueuedJobs"] = 1
        save_json(self.state / "queue/blocker.json", self.job("blocker", project="q"))
        enqueue_job(self.job("source"), self.state / "queue/source.json", self.config)
        pending_path = self.state / "capture-pending/source.json"
        pending = load_json(pending_path)
        (self.state / "queue/blocker.json").unlink()
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        original_unlink = Path.unlink
        failed_once = False

        def interrupt_pending_unlink(path, *args, **kwargs):
            nonlocal failed_once
            if path == pending_path and not failed_once:
                failed_once = True
                raise OSError("simulated transfer interruption")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupt_pending_unlink):
            with self.assertRaisesRegex(OSError, "transfer interruption"):
                process_capture_retries(self.config, due)

        self.assertEqual(admission_usage(self.state)["total"], 1)
        self.assertTrue(pending_path.exists())
        self.assertTrue((self.state / "queue/source.json").exists())
        self.assertEqual(process_capture_retries(self.config, due)["recovered"], 1)
        self.assertFalse(pending_path.exists())
        self.assertTrue((self.state / "queue/source.json").exists())

    def test_deterministic_error_is_failed_once_with_full_result_and_no_review(self):
        """A permanent technical rejection is retained in the batch and never retried."""
        source = self.job("bad")
        save_json(self.state / "queue/bad.json", source)
        result_value = {"status": "error", "retryable": False, "reviewCount": 0,
                       "error": "invalid accepted review", "claimReviews": [{"decision": "reject"}]}
        calls = []
        invoke = lambda *_args: calls.append(True) or result_value

        process_queue(self.config, invoke, clock=lambda: self.now)
        second = process_queue(self.config, invoke, clock=lambda: self.now + dt.timedelta(seconds=400))
        audit = load_json(next((self.state / "batches").glob("*.json")))

        self.assertEqual(second["attempts"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["result"], result_value)
        self.assertEqual(load_json(self.state / "failed/bad.json")["evidence"], source["evidence"])
        self.assertFalse((self.state / "completed/bad.json").exists())
        self.assertFalse((self.state / "review/bad.json").exists())

    def test_capacity_snapshot_drift_does_not_restore_corrupt_input(self):
        """A changed snapshot hash fails closed and remains visible for diagnosis."""
        self.config["maxQueuedJobs"] = 1
        save_json(self.state / "queue/blocker.json", self.job("blocker", project="q"))
        enqueue_job(self.job("source"), self.state / "queue/source.json", self.config)
        path = self.state / "capture-pending/source.json"
        pending = load_json(path)
        pending["job"]["evidence"][0]["text"] = "changed"
        save_json(path, pending)

        report = process_capture_retries(self.config, self.now + dt.timedelta(days=10))

        self.assertEqual(report["recovered"], 0)
        self.assertTrue(path.exists())
        self.assertFalse((self.state / "queue/source.json").exists())
        self.assertFalse((self.state / "failed/source.json").exists())

    def test_invalid_capacity_queue_filename_remains_pending_and_counted(self):
        """Malformed or unbound destination metadata never escapes or bypasses intake."""
        self.config["maxQueuedJobs"] = 1
        blocker = self.state / "queue/blocker.json"
        save_json(blocker, self.job("blocker", project="q"))
        enqueue_job(self.job("source"), self.state / "queue/source.json", self.config)
        pending_path = self.state / "capture-pending/source.json"
        pending = load_json(pending_path)
        blocker.unlink()
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        invalid_names = ["../orphan/source.json", str(self.root / "outside.json"),
                         "nested/source.json", "unrelated.json", "source.txt", "", None,
                         [], ["source.json"], {}, {"path": "source.json"}]
        for name in invalid_names:
            with self.subTest(queue_file=name):
                save_json(pending_path, {**pending, "queueFile": name})
                before = pending_path.read_bytes()
                self.assertEqual(admission_usage(self.state)["waiting"], 1)
                result = process_capture_retries(self.config, due)
                self.assertEqual((result["recovered"], result["invalid"]), (0, 1))
                self.assertEqual(load_json(self.state / "capture-errors/source.json")["type"],
                                 "CapacitySnapshotInvalid")
                self.assertEqual(pending_path.read_bytes(), before)
                self.assertEqual(list((self.state / "queue").glob("*.json")), [])
                self.assertFalse((self.state / "orphan/source.json").exists())
                self.assertFalse((self.root / "outside.json").exists())

    def test_parked_legacy_filename_is_bound_to_the_frozen_source(self):
        """An existing safe queue name survives parking without becoming mutable metadata."""
        queued = self.state / "queue/legacy-source.json"
        save_json(queued, self.job("source"))
        hold = self.state / "review/p-held.json"
        save_json(hold, {"jobId": "p-held", "projectId": "p"})
        process_queue(self.config, lambda *_args: self.fail("blocked source invoked"),
                      clock=lambda: self.now)
        pending = load_json(self.state / "capture-pending/source.json")
        self.assertEqual(pending["job"]["queueFile"], queued.name)
        self.assertEqual(pending["queueFile"], queued.name)
        hold.unlink()
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        self.assertEqual(process_capture_retries(self.config, due)["recovered"], 1)
        self.assertEqual(load_json(queued)["id"], "source")
        self.assertFalse((self.state / "capture-pending/source.json").exists())


if __name__ == "__main__":
    unittest.main()
