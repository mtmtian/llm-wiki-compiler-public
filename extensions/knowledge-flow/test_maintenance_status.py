"""Exercise the persisted status command through its real CLI boundary."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from common import save_json
from replica import initialize_baseline
from review_capacity import QUEUE_FULL_ERROR


class MaintenanceStatusTests(unittest.TestCase):
    """Status must report durable health while leaving the observed state untouched."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.config = self.root / "config.json"
        save_json(self.config, {"version": 1, "wikiRoot": str(self.root / "wiki"),
                  "stateDir": str(self.state), "worker": str(self.root / "worker.mjs"),
                  "node": "/usr/bin/node", "projects": {}, "enabled": False})
        save_json(self.state / "replica/status.json", {"digest": "legacy", "count": 1})

    def write_audit(self, job_id, error, recorded_at):
        save_json(self.state / "audit" / f"{job_id}.json", {
            "status": "needs_review", "error": error, "jobId": job_id,
            "projectId": "synthetic", "recordedAt": recorded_at})
        save_json(self.state / "batches" / f"{job_id}.json", {
            "batchId": job_id, "status": "completed", "job": {"id": job_id},
            "result": {"status": "needs_review", "error": error}})

    def seed_status_scenarios(self):
        """Persist queue-full holds that are open, dismissed, continued, and dangling."""
        self.write_audit("open", QUEUE_FULL_ERROR, "2026-10-02T10:00:00Z")
        self.write_audit("dismissed", QUEUE_FULL_ERROR, "2026-10-02T11:00:00Z")
        save_json(self.state / "resolved/dismissed.json", {"action": "dismiss", "anchor": "audit"})
        self.write_audit("continued", QUEUE_FULL_ERROR, "2026-10-03T10:00:00Z")
        save_json(self.state / "resolved/continued.json", {"action": "reprocessed",
                  "retryJobId": "review-continued", "result": {"status": "needs_review"}})
        self.write_audit("published", QUEUE_FULL_ERROR, "2026-10-03T10:30:00Z")
        save_json(self.state / "resolved/published.json", {"action": "reprocessed",
                  "retryJobId": "review-published", "result": {"status": "published"}})
        self.write_audit("new-review", QUEUE_FULL_ERROR, "2026-10-03T11:00:00Z")
        save_json(self.state / "resolved/new-review.json", {"action": "reprocessed",
                  "retryJobId": "review-new-review", "result": {"status": "needs_review"}})
        save_json(self.state / "review/review-new-review.json", {"jobId": "review-new-review"})
        self.seed_review_successor("dismissed-successor", "review-successor-dismissed", "dismiss")
        self.seed_review_successor("retried-successor", "review-successor-retried", "reprocessed")
        self.write_audit("later", "TypeError", "2026-10-03T12:00:00Z")

    def seed_review_successor(self, source_id, retry_id, action):
        """Persist a retry that reached a newer review with its own final disposition."""
        self.write_audit(source_id, QUEUE_FULL_ERROR, "2026-10-03T13:00:00Z")
        save_json(self.state / "resolved" / f"{source_id}.json", {"action": "reprocessed",
                  "retryJobId": retry_id, "result": {"status": "needs_review"}})
        successor = {"action": action, "review": {"jobId": retry_id}}
        if action == "reprocessed":
            successor["retryJobId"] = "review-next"
            successor["result"] = {"status": "needs_review"}
        save_json(self.state / "resolved" / f"{retry_id}.json", successor)

    def file_snapshot(self):
        """Capture all state bytes and mtimes to detect every CLI side effect."""
        return {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def configure_enabled_replica_probe(self):
        """Prepare a sync-capable v2 configuration whose fake node leaves a detectable mark."""
        shared = self.root / "shared-wiki"
        shared.mkdir()
        marker = self.root / "node-called"
        node = self.root / "probe-node"
        node.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n", encoding="utf-8")
        node.chmod(0o755)
        config = {"version": 1, "machineId": "synthetic", "wikiRoot": str(self.state / "replica/current"),
                  "sharedWikiRoot": str(shared), "stateDir": str(self.state), "worker": str(self.root / "worker.mjs"),
                  "node": str(node), "projects": {}, "enabled": True, "intakeEnabled": False,
                  "publishEnabled": False, "exchange": {"protocolVersion": 2,
                  "root": str(self.root / "exchange"), "participants": ["synthetic"],
                  "materializerMachineId": "synthetic"}}
        save_json(self.config, config)
        initialize_baseline(config)
        return marker

    def run_status(self, *options):
        command = [sys.executable, "-B", str(Path(__file__).with_name("maintenance.py")),
                   "--config", str(self.config), "--status", *options]
        return subprocess.run(command, capture_output=True, text=True, check=True)

    def test_status_is_read_only_and_excludes_only_confirmed_continuations(self):
        """Given persisted errors, When status runs, Then it reports holds without pruning or writing."""
        marker = self.configure_enabled_replica_probe()
        self.seed_status_scenarios()
        save_json(self.state / "review/old.json", {"jobId": "old", "projectId": "synthetic"})
        save_json(self.state / "review/retry.json", {
            "jobId": "retry", "projectId": "synthetic", "reviewRetryOf": "old"})
        save_json(self.state / "review/independent.json", {
            "jobId": "independent", "projectId": "other"})
        old_turn = self.state / "turns/expired.json"
        save_json(old_turn, {"prompt": "synthetic"})
        expired = time.time() - 8 * 86400
        os.utime(old_turn, (expired, expired))
        before = self.file_snapshot()

        result = json.loads(self.run_status().stdout)

        after = self.file_snapshot()
        self.assertEqual(after, before)
        self.assertEqual(result["audit"]["unresolvedQueueFullCount"], 2)
        self.assertEqual({item["jobId"] for item in result["audit"]["unresolvedQueueFull"]}, {"open", "continued"})
        self.assertEqual(result["audit"]["errorsByType"][QUEUE_FULL_ERROR], 7)
        self.assertEqual(result["audit"]["errorsByDay"]["2026-10-03"], 6)
        self.assertEqual(result["counts"]["review"], 4)
        self.assertEqual(result["activeReviewCount"], 2)
        self.assertEqual({item["jobId"] for item in result["activeReviews"]}, {"retry", "independent"})
        self.assertIsNone(result["lastSuccessfulSyncAt"])
        self.assertFalse((self.state / "maintenance.json").exists())
        self.assertFalse(marker.exists())
        self.assertFalse((self.state / "replica.lock").exists())

    def test_status_rejects_combining_with_a_write_operation(self):
        """A status request cannot silently fall through to a drain or a snapshot write."""
        command = [sys.executable, "-B", str(Path(__file__).with_name("maintenance.py")),
                   "--config", str(self.config), "--status", "--drain"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--status cannot be combined", result.stderr)
        self.assertFalse((self.state / "maintenance.json").exists())


if __name__ == "__main__":
    unittest.main()
