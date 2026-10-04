"""A durable finalization backoff must preserve session order without stalling peers."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import load_json, save_json
from queue_worker import process_queue


class QueueProgressTests(unittest.TestCase):
    """Exercise real durable batches and queue files with only the model faked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.config = {"enabled": True, "stateDir": str(self.state), "maxDailyJobs": 100,
                       "projects": {"p": {"pages": []}, "q": {"pages": []}}}

    def put(self, name, project="p", session="one"):
        job = {"id": name, "projectId": project, "sessionId": session,
               "evidence": [{"id": name, "kind": "user", "text": name}]}
        save_json(self.state / "queue" / (name + ".json"), job)
        return job

    def pending_result(self):
        job = self.put("a")
        audit = {"batchId": "batch-a", "status": "finalize-retry", "queueFiles": ["a.json"],
                 "job": {**job, "id": "batch-a", "sourceJobIds": ["a"]},
                 "result": {"status": "empty"},
                 "nextFinalizeAt": (self.now + timedelta(hours=1)).isoformat()}
        save_json(self.state / "batches/batch-a.json", audit)
        self.put("b")
        self.put("c", session="two")
        self.put("d", project="q")
        return audit

    def test_backoff_preserves_same_session_order_and_drains_independent_work(self):
        """Given a frozen result, When it is not due, Then peers complete and its session waits."""
        audit = self.pending_result()
        result = process_queue(self.config, lambda *_: {"status": "empty"}, clock=lambda: self.now)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(sorted(p.stem for p in (self.state / "queue").glob("*.json")), ["a", "b"])
        self.assertEqual(load_json(self.state / "batches/batch-a.json"), audit)
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 2)
        process_queue(self.config, lambda *_: {"status": "empty"},
                      clock=lambda: self.now + timedelta(hours=2))
        self.assertFalse(list((self.state / "queue").glob("*.json")))
        self.assertEqual(load_json(self.state / "batches/batch-a.json")["status"], "completed")

    def test_new_finalization_failure_does_not_stall_other_sessions(self):
        """Given a new durable output, When receipt IO fails, Then only that session waits."""
        self.put("a")
        self.put("c", session="two")
        def receipt(_config, job, _result):
            if job["sessionId"] == "one":
                raise OSError("receipt temporarily unavailable")
        with patch("queue_finalization.write_receipt", side_effect=receipt):
            result = process_queue(self.config, lambda *_: {"status": "empty"}, clock=lambda: self.now)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(sorted(p.stem for p in (self.state / "queue").glob("*.json")), ["a"])
        self.assertTrue((self.state / "completed/c.json").exists())
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 2)

    def test_deferred_sessions_do_not_exhaust_the_wake_work_limit(self):
        """Given more backoffs than the work limit, When woken, Then a due peer still runs."""
        for index in range(4):
            job = self.put(str(index), session=str(index))
            save_json(self.state / "batches" / ("batch-" + str(index) + ".json"), {
                "batchId": "batch-" + str(index), "status": "finalize-retry",
                "queueFiles": [str(index) + ".json"], "job": job,
                "result": {"status": "empty"},
                "nextFinalizeAt": (self.now + timedelta(hours=1)).isoformat()})
        self.put("healthy", session="healthy")
        result = process_queue(self.config, lambda *_: {"status": "empty"},
                               limit=1, clock=lambda: self.now)
        self.assertEqual(result["processed"], 1)
        self.assertTrue((self.state / "completed/healthy.json").exists())
        self.assertEqual(len(list((self.state / "queue").glob("*.json"))), 4)
        self.assertEqual(load_json(self.state / "daily-budget.json")["used"], 1)


if __name__ == "__main__":
    unittest.main()
