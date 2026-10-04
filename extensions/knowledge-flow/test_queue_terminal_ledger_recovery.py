"""Recover an accepted ledger result after terminal retry was interrupted.

Given a retryable error carrying accepted ledger contribution and multiple
queue sources at their final retry, a process stop during the terminal source
moves must retain the last accepted result. Recovery must export that ledger
once, quarantine the sources, and never invoke the worker again.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import load_json, save_json
from queue_recovery import retry_job
from queue_worker import process_queue


class TerminalLedgerRecoveryTests(unittest.TestCase):
    """Use real queue and batch files while faking only worker and export edges."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.state),
                       "projects": {"p": {"pages": []}}, "maxDailyJobs": 100}
        for name in ("a", "b"):
            save_json(self.state / "queue" / f"{name}.json", {
                "id": name, "projectId": "p", "sessionId": "s", "prompt": name,
                "evidence": [{"id": name, "kind": "user", "text": name}],
                "allowedPageIds": []})

    def test_last_result_ledger_replays_after_partial_terminal_move(self):
        accepted = {"status": "error", "retryable": True, "error": "phase warning",
                    "ledgerContribution": {"version": 1, "records": []}}

        process_queue(self.config, lambda *args: accepted, clock=lambda: self.now)
        process_queue(self.config, lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
                      clock=lambda: self.now + timedelta(seconds=301))

        def interrupt_after_first_move(state, path, job, now, config):
            if path.name == "a.json":
                retry_job(state, path, job, now, config)
                return
            raise KeyboardInterrupt()

        with patch("queue_finalization._retry_job", side_effect=interrupt_after_first_move):
            with self.assertRaises(KeyboardInterrupt):
                process_queue(self.config, lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
                              clock=lambda: self.now + timedelta(seconds=902))

        audit_path = next((self.state / "batches").glob("*.json"))
        interrupted = load_json(audit_path)
        self.assertEqual(interrupted["status"], "failure-finalize")
        self.assertEqual(interrupted["result"], accepted)

        calls, exports = [], []
        with patch("queue_finalization.export_result",
                   side_effect=lambda config, job, result: exports.append(result) or result):
            process_queue(self.config,
                          lambda *args: calls.append(args) or {"status": "empty"},
                          clock=lambda: self.now + timedelta(seconds=903))

        self.assertEqual(calls, [])
        self.assertEqual(exports, [accepted])
        self.assertEqual(load_json(audit_path)["status"], "failed")
        self.assertFalse((self.state / "completed/a.json").exists())
        self.assertFalse((self.state / "completed/b.json").exists())
        self.assertTrue((self.state / "failed/a.json").exists())
        self.assertTrue((self.state / "failed/b.json").exists())


if __name__ == "__main__":
    unittest.main()
