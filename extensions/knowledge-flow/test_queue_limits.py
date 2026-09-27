"""Regression tests for durable replay and the subprocess wire boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json, save_json
from hooks import invoke, prompt_event
from queue_worker import process_queue
import test_replica


class QueueLimitTests(unittest.TestCase):
    """Oversize intake must fail visibly without losing durable work."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "projects": {"growth": {"pages": []}}, "maxDailyJobs": 100}

    def tearDown(self):
        self.temp.cleanup()

    def job(self, name="job"):
        return {"id": name, "projectId": "growth", "sessionId": "session", "prompt": name,
                "evidence": [{"id": name, "kind": "user", "text": name}]}

    def worker_config(self):
        """Use a real subprocess to observe the exact bytes and decoded request."""
        worker = self.root / "worker.py"
        worker.write_text(
            "import json,sys\nfrom pathlib import Path\nraw=sys.stdin.buffer.read()\n"
            "v=json.loads(raw)\n"
            f"Path({str(self.root / 'invoked')!r}).write_text(str(len(raw)))\n"
            "print(json.dumps({'status':'empty','bytes':len(raw),'request':v}))\n", encoding="utf-8")
        return {**self.config, "node": sys.executable, "worker": str(worker)}

    def test_durable_result_is_replayed_before_new_size_policy(self):
        """A saved result remains replayable even if its source later exceeds intake caps."""
        job = self.job("durable")
        job["evidence"] = [{"id": "large", "kind": "user", "text": "x" * 130_000}]
        save_json(self.root / "queue/durable.json", job)
        merged = {**job, "id": "batch-durable", "sourceJobIds": ["durable"],
                  "sourceQueueFiles": ["durable.json"]}
        save_json(self.root / "batches/batch-durable.json", {
            "batchId": "batch-durable", "status": "finalize-retry", "queueFiles": ["durable.json"],
            "job": merged, "result": {"status": "empty"},
            "nextFinalizeAt": (self.now + timedelta(hours=1)).isoformat()})
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["reason"], "finalize-backoff")
        self.assertTrue((self.root / "queue/durable.json").exists())
        self.assertEqual(load_json(self.root / "batches/batch-durable.json")["status"], "finalize-retry")
        process_queue(self.config, lambda *args: calls.append(args),
                      clock=lambda: self.now + timedelta(hours=2))
        self.assertEqual(load_json(self.root / "completed/durable.json"), {"status": "empty"})
        self.assertEqual(load_json(self.root / "batches/batch-durable.json")["status"], "completed")
        self.assertFalse(calls)
        self.assertFalse((self.root / "failed/durable.json").exists())

    def test_terminal_retry_move_is_restart_safe(self):
        """Given a failed terminal move, When the worker restarts, Then it never invokes again."""
        from queue_recovery import os as recovery_os
        self.config["retryBaseSeconds"] = 1
        job = self.job("terminal")
        job["attempts"] = 2
        save_json(self.root / "queue/terminal.json", job)
        calls = []
        real_replace = recovery_os.replace
        failed_once = False

        def fail_terminal_move(source, destination):
            nonlocal failed_once
            if not failed_once and str(destination).endswith("failed/terminal.json"):
                failed_once = True
                raise OSError("simulated terminal move interruption")
            return real_replace(source, destination)

        with patch("queue_recovery.os.replace", side_effect=fail_terminal_move):
            with self.assertRaises(OSError):
                process_queue(self.config, lambda *args: calls.append(args) or {"status": "error"},
                              clock=lambda: self.now)
        self.assertEqual(len(calls), 1)
        self.assertEqual(load_json(self.root / "batches" / next((self.root / "batches").iterdir()).name)["status"], "failed")
        second = process_queue(self.config, lambda *args: calls.append(args) or {"status": "empty"},
                               clock=lambda: self.now + timedelta(seconds=2))
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["attempts"], 0)
        self.assertFalse((self.root / "queue/terminal.json").exists())
        self.assertEqual(load_json(self.root / "failed/terminal.json")["attempts"], 3)

    def test_process_wire_limit_rejects_before_injected_invoke(self):
        """Queue preflight uses the same hard limit before any model callback."""
        self.config["maxProcessEventBytes"] = 1_000
        job = self.job("large")
        job["evidence"] = [{"id": "large", "kind": "user", "text": "中文" * 1_000}]
        save_json(self.root / "queue/large.json", job)
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["results"][0]["reason"], "oversize")
        self.assertTrue((self.root / "failed/large.json").exists())
        self.assertFalse((self.root / "daily-budget.json").exists())

    def test_partial_terminal_batch_preserves_basis_without_regrouping(self):
        """Given a hard stop after one failed move, recovery keeps the frozen batch terminal."""
        first, second = self.job("a"), self.job("b")
        first["attempts"] = 3
        second["attempts"] = 1
        save_json(self.root / "failed/a.json", first)
        save_json(self.root / "queue/b.json", second)
        basis = {"basisRecordIds": ["original"], "generation": "old", "wikiRoot": "/old"}
        audit_path = self.root / "batches/batch-frozen.json"
        save_json(audit_path, {"batchId": "batch-frozen", "status": "retry",
                              "queueFiles": ["a.json", "b.json"], "replicaBasis": basis,
                              "job": {**first, "id": "batch-frozen"}})
        for days in (1, 2, 3):
            result = process_queue(self.config, self.forbid_model,
                                   clock=lambda: self.now + timedelta(days=days))
            self.assertEqual(result["attempts"], 0)
        self.assertFalse(list((self.root / "queue").glob("*.json")))
        self.assertEqual(load_json(self.root / "failed/b.json")["status"], "batch-failed")
        audit = load_json(audit_path)
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["replicaBasis"], basis)
        self.assertEqual(len(list((self.root / "batches").glob("*.json"))), 1)

    @staticmethod
    def forbid_model(*_args):
        raise AssertionError("terminal batches must not invoke the model")

    def test_invoke_rejects_oversized_context_even_when_configured_cap_is_larger(self):
        """Context requests cannot bypass the Node hard cap through configuration."""
        config = {**self.worker_config(), "maxProcessEventBytes": 900_000}
        with self.assertRaisesRegex(ValueError, "event.*bytes"):
            invoke(config, "context", {"projectId": "growth", "prompt": "中文" * 110_000}, 1)
        self.assertFalse((self.root / "invoked").exists())

    def test_unicode_queue_payload_uses_utf8_bytes_without_ascii_expansion(self):
        """Given legal Chinese/emoji evidence, When sent, Then exact UTF-8 fits and text survives."""
        config = {**self.worker_config(), "maxJobBytes": 320_000, "maxBatchBytes": 320_000}
        job = self.job()
        job["evidence"][0]["text"] = "中\U0001f600" * 40_000
        save_json(self.root / "queue/job.json", job)
        result = process_queue(config, clock=lambda: self.now)
        completed = load_json(self.root / "completed/job.json")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(completed["request"]["job"]["evidence"], job["evidence"])
        self.assertLess(completed["bytes"], 600_000)

    def test_exact_wire_limit_includes_config_and_envelope(self):
        """Given a UTF-8 request at its cap, When one byte is added, Then it is rejected."""
        config = {**self.worker_config(), "maxProcessEventBytes": 2000}
        payload = {"prompt": "中\U0001f600"}
        size = len(json.dumps({**payload, "config": config}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        payload["prompt"] += "x" * (2000 - size)
        self.assertEqual(invoke(config, "context", payload, 5)["bytes"], 2000)
        (self.root / "invoked").unlink()
        payload["prompt"] += "x"
        with self.assertRaisesRegex(ValueError, "2001 bytes"):
            invoke(config, "context", payload, 5)
        self.assertFalse((self.root / "invoked").exists())

    def test_large_page_registry_is_rejected_at_context_boundary(self):
        """Given a large accepted-page scope, When context loads, Then no oversized subprocess runs."""
        config = {**self.worker_config(), "maxProcessEventBytes": 600_000}
        allowed = [f"concepts/{i}-" + "a" * 60 for i in range(12000)]
        with self.assertRaisesRegex(ValueError, "context event"):
            invoke(config, "context", {"projectId": "growth", "prompt": "review decision",
                                         "allowedPageIds": allowed, "seen": {}}, 5)
        self.assertFalse((self.root / "invoked").exists())

    def test_replica_scope_growth_is_checked_after_preparation_without_budget(self):
        """Given a small source, When replica adds page scope, Then final wire is bounded before budget."""
        fixture = test_replica.ReplicaTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = {**fixture.configs["a"], "enabled": True, "intakeEnabled": True,
                  "maxProcessEventBytes": 5000}
        state = Path(config["stateDir"])
        save_json(state / "queue/job.json", {**self.job(), "projectId": "project"})
        def materialize(_config, stage, _records):
            for index in range(80):
                name = "project-" + digest("project")[:8] + "-" + str(index) + "x" * 70
                (stage / "wiki/concepts" / (name + ".md")).write_text("Accepted")
            return {"pages": 80, "conflicts": []}
        calls = []
        with patch("replica_generation._invoke_materializer", side_effect=materialize):
            result = process_queue(config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["reason"], "oversize")
        self.assertFalse((state / "daily-budget.json").exists())
        self.assertTrue((state / "failed/job.json").exists())
        audit = load_json(next((state / "batches").glob("*.json")))
        self.assertIn("replicaBasis", audit)
        self.assertGreater(audit["wireBytes"], 5000)


if __name__ == "__main__":
    unittest.main()
