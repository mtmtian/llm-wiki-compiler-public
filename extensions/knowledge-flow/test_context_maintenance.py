"""Given/When/Then tests for private Wiki read diagnostics in maintenance."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import maintenance


class ContextMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = {"stateDir": str(self.state), "projects": {}, "enabled": False}

    def tearDown(self):
        self.temp.cleanup()

    def write_diag(self, name, value, age=0):
        folder = self.state / "context-diagnostics"
        folder.mkdir(exist_ok=True)
        path = folder / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")
        stamp = time.time() - age * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_recent_diagnostics_are_aggregated_without_query_or_prompt(self):
        """Given recent private records, When reporting, Then only status counts and latest time are exposed."""
        self.write_diag("ok.json", {"kind": "turn", "status": "ok", "preparedEvidence": True,
                                     "preparedCount": 2, "observedAt": "2026-09-18T10:00:00Z",
                                     "queryHash": "secret-query", "prompt": "private prompt",
                                     "stopObservedAt": "2026-09-18T10:01:00Z",
                                     "stopMessageObserved": True, "explicitReference": True})
        self.write_diag("empty.json", {"kind": "turn", "status": "no-scope", "preparedEvidence": False,
                                        "at": "2026-09-18T11:00:00Z"})
        self.write_diag("degraded.json", {"kind": "turn", "status": "degraded", "preparedEvidence": True,
                                           "at": "2026-09-18T12:00:00Z", "errorType": "TimeoutExpired",
                                           "stopObservedAt": "2026-09-18T12:01:00Z",
                                           "stopMessageObserved": True, "explicitReference": False})
        self.write_diag("lifecycle.json", {"kind": "lifecycle", "status": "ok",
                                            "preparedEvidence": True, "at": "2026-09-18T13:00:00Z"})
        self.write_diag("legacy.json", {"status": "ok", "prepared": True,
                                         "at": "2026-09-18T10:00:00Z"})
        self.write_diag("old.json", {"status": "ok", "prepared": True}, age=8)
        self.write_diag("broken.json", "{")

        result = maintenance.report(self.config)
        summary = result["contextRead"]
        self.assertEqual(summary["counts"], {"ok": 1, "no-scope": 1, "degraded": 1})
        self.assertEqual(summary["preparedEvidenceTurns"], 2)
        self.assertEqual(summary["prepared"], 2)
        self.assertEqual(summary["matchingStopTurns"], 2)
        self.assertEqual(summary["finalMessageObservedTurns"], 2)
        self.assertEqual(summary["explicitReferenceTurns"], 1)
        self.assertEqual(summary["referenceUnknownTurns"], 0)
        self.assertEqual(summary["lifecycleRecords"], 1)
        self.assertEqual(summary["legacyRecords"], 1)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["latestObservedAt"], "2026-09-18T13:00:00Z")
        self.assertEqual(summary["delivery"], "unverified")
        self.assertEqual(summary["adoption"], "unverified")
        self.assertNotIn("private prompt", json.dumps(result))
        self.assertNotIn("secret-query", json.dumps(result))

    def test_prune_turns_expires_old_context_diagnostics(self):
        old = self.write_diag("old.json", {"status": "ok"}, age=8)
        recent = self.write_diag("recent.json", {"status": "ok"})
        maintenance.prune_turns(self.state)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_post_preparation_counts_exclude_no_scope_and_keep_missing_stop_unknown(self):
        """Given no-scope has a Stop but evidence has none, When summarized, Then only evidence counts as unknown."""
        self.write_diag("no-scope.json", {"kind": "turn", "status": "no-scope",
                                           "preparedEvidence": False, "stopObservedAt": "now",
                                           "stopMessageObserved": True, "explicitReference": True})
        self.write_diag("prepared.json", {"kind": "turn", "status": "ok", "preparedEvidence": True,
                                           "preparedCount": 1})
        summary = maintenance.context_read_summary(self.state)
        self.assertEqual(summary["preparedEvidenceTurns"], 1)
        self.assertEqual(summary["matchingStopTurns"], 0)
        self.assertEqual(summary["finalMessageObservedTurns"], 0)
        self.assertEqual(summary["explicitReferenceTurns"], 0)
        self.assertEqual(summary["referenceUnknownTurns"], 1)


if __name__ == "__main__":
    unittest.main()
