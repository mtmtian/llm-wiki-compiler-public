"""Wake-state tests: quiet runs stay quiet while due work drains once."""

import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import load_json, save_json
from capture_retry import record_capture_pending
from wake import run_once, run_with_debounce


class WakeTests(unittest.TestCase):
    """Exercise the one-shot reconciler without a real model or exchange."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "eventDriven": {"enabled": True, "debounceSeconds": 120},
                       "projects": {"growth": {"pages": []}}}

    def tearDown(self):
        self.temp.cleanup()

    def test_quiet_wake_does_not_rewrite_state(self):
        first = run_once(self.config, invoke=lambda *args: {"status": "empty"}, clock=lambda: self.now)
        path = self.root / "wake-state.json"
        stamp = path.stat().st_mtime_ns
        time.sleep(0.001)
        second = run_once(self.config, invoke=lambda *args: {"status": "empty"}, clock=lambda: self.now)
        self.assertFalse(second["changed"])
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(first["counters"], second["counters"])

    def test_due_queue_is_drained_and_status_changes(self):
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth",
                                                   "sessionId": "s", "prompt": "decision",
                                                   "evidence": [], "notBefore": self.now.isoformat()})
        calls = []
        result = run_once(self.config, invoke=lambda *args: calls.append(args) or {"status": "empty"},
                          clock=lambda: self.now)
        self.assertEqual(result["counters"]["processed"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(load_json(self.root / "wake-state.json")["counters"]["queue"], 0)

    def test_short_debounce_waits_then_drains_without_losing_turn(self):
        current = [self.now]
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth",
                                                   "sessionId": "s", "prompt": "decision",
                                                   "evidence": [], "createdAt": self.now.isoformat()})
        calls = []
        def sleep(seconds):
            current[0] += timedelta(seconds=seconds)
        result = run_with_debounce(self.config, invoke=lambda *args: calls.append(args) or {"status": "empty"},
                                   clock=lambda: current[0], sleep_fn=sleep)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["counters"]["processed"], 1)

    def test_disabled_event_worker_never_invokes(self):
        self.config["eventDriven"] = {"enabled": False}
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth",
                                                   "sessionId": "s", "prompt": "decision",
                                                   "evidence": []})
        calls = []
        result = run_once(self.config, invoke=lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertIn("event-disabled", result["reasons"])

    def test_disabled_wake_never_touches_exchange_or_waits_for_debounce(self):
        self.config["eventDriven"] = {"enabled": False}
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth",
                                                "createdAt": self.now.isoformat()})
        with patch("wake.exchange_counts") as counts, patch("wake.announce_machine") as announce:
            result = run_with_debounce(self.config, clock=lambda: self.now, announce=True,
                                      sleep_fn=lambda _: self.fail("disabled wake slept"))
        counts.assert_not_called()
        announce.assert_not_called()
        self.assertIsNone(result["nextWakeAt"])

    def test_idle_v2_event_reconciles_replica_without_a_model(self):
        """An idle v2 wake still syncs its replica, with no model attempt."""
        self.config["exchange"] = {"protocolVersion": 2}
        self.config["intakeEnabled"] = False
        with patch("wake._reconcile_replica") as reconcile, patch("wake.exchange_counts", return_value={}):
            result = run_once(self.config, invoke=lambda *_: self.fail("empty wake invoked model"))
        reconcile.assert_called_once_with(self.config)
        self.assertEqual(result["counters"]["processed"], 0)
        self.assertEqual(result["counters"]["attempts"], 0)

    def test_session_waits_300_seconds_then_later_wake_drains_one_batch(self):
        """Turns stay queued before quiet expiry, then one later wake merges the session."""
        queued = (("first", self.now), ("second", self.now + timedelta(seconds=60)))
        for identifier, queued_at in queued:
            save_json(self.root / "queue" / f"{identifier}.json", {
                "id": identifier, "projectId": "growth", "sessionId": "s",
                "prompt": identifier, "evidence": [], "createdAt": queued_at.isoformat(),
                "sessionContext": {"version": 1, "revision": 0, "summary": "",
                                   "topicPageIds": [], "evidence": []},
                "sessionSchedule": {"version": 1, "firstQueuedAt": self.now.isoformat(),
                                    "lastQueuedAt": queued_at.isoformat(), "explicit": False},
            })
        calls = []
        before_due = run_once(self.config, invoke=lambda *args: calls.append(args[2]["job"]),
                              clock=lambda: self.now + timedelta(seconds=359))
        self.assertFalse(calls)
        self.assertEqual(before_due["counters"]["queue"], 2)
        self.assertEqual(before_due["nextWakeAt"], (self.now + timedelta(seconds=360)).isoformat())

        due = run_once(self.config, invoke=lambda *args: calls.append(args[2]["job"]) or {"status": "empty"},
                       clock=lambda: self.now + timedelta(seconds=360))
        self.assertEqual([item["sourceJobIds"] for item in calls], [["first", "second"]])
        self.assertEqual(due["counters"]["processed"], 2)

    def test_replica_failure_preserves_queue_and_surfaces_processing_error(self):
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth", "evidence": []})
        with patch("wake._reconcile_replica", side_effect=OSError("unavailable")):
            result = run_once(self.config, invoke=lambda *_: self.fail("unsynced model invoked"))
        self.assertIn("processing-error", result["reasons"])
        self.assertEqual(result["counters"]["queue"], 1)
        self.assertEqual(result["counters"]["attempts"], 0)

    def test_replica_failure_does_not_prevent_local_capture_expiry(self):
        """Given an unavailable native record, a sync outage cannot stall bounded recovery."""
        event = {"session_id": "s", "turn_id": "t", "cwd": str(self.root),
                 "transcript_path": str(self.root / "sessions/missing.jsonl")}
        record_capture_pending(self.config, "capture", event, {"projectId": "growth"},
                               {"evidence": []}, "unavailable", self.now)
        save_json(self.root / "queue/job.json", {"id": "job", "projectId": "growth", "evidence": []})
        with patch("wake._reconcile_replica", side_effect=OSError("unavailable")):
            result = run_once(self.config, invoke=lambda *_: self.fail("unsynced model invoked"),
                              clock=lambda: self.now + timedelta(hours=1))
        self.assertEqual(result["drain"]["captureRecovery"]["expired"], 1)
        self.assertFalse((self.root / "capture-pending/capture.json").exists())
        self.assertTrue((self.root / "capture-errors/capture.json").exists())
        self.assertTrue((self.root / "queue/job.json").exists())
        self.assertEqual(result["counters"]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
