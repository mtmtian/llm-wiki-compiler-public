"""Isolation tests for durable event-driven queue processing."""

import tempfile
import unittest
import fcntl
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import load_json, save_json
from queue_worker import process_queue
from replica_integrity import seal_generation


class QueueWorkerTests(unittest.TestCase):
    """Keep model invocation and private queue state fully deterministic."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "projects": {"growth": {"pages": []}, "mobile": {"pages": []}},
                       "maxDailyJobs": 12}

    def tearDown(self):
        self.temp.cleanup()

    def job(self, name, project="growth", session="s", **extra):
        value = {"id": name, "projectId": project, "sessionId": session,
                 "prompt": name, "evidence": [{"id": name, "kind": "user", "text": name}],
                 "allowedPageIds": []}
        value.update(extra)
        return value

    def put(self, name, **kwargs):
        save_json(self.root / "queue" / (name + ".json"), self.job(name, **kwargs))

    def test_empty_queue_does_not_invoke(self):
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["processed"], 0)
        self.assertFalse(calls)

    def test_receipt_only_reconcile_does_not_invoke(self):
        self.config["exchange"] = {"root": str(self.root / "exchange"),
                                    "publisherMachineId": "publisher", "participants": ["publisher"]}
        self.config.update(machineId="publisher", publishEnabled=True)
        (self.root / "exchange/submissions/publisher").mkdir(parents=True)
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["processed"], 0)
        self.assertFalse(calls)

    def test_existing_exchange_queue_with_receipt_is_retired_without_model(self):
        self.config["exchange"] = {"root": str(self.root / "exchange"),
                                    "publisherMachineId": "publisher", "participants": ["publisher"]}
        self.config.update(machineId="publisher", publishEnabled=True)
        identifier = "a" * 64
        save_json(self.root / "queue" / ("exchange-" + identifier + ".json"),
                  self.job("exchange-" + identifier))
        save_json(self.root / "exchange/receipts" / (identifier + ".json"),
                  {"submissionId": identifier, "publisherMachineId": "publisher",
                   "status": "published", "publishedPageIds": []})
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["receipts"], 1)
        self.assertFalse(calls)
        self.assertFalse((self.root / "queue" / ("exchange-" + identifier + ".json")).exists())

    def test_same_session_project_is_one_batch(self):
        self.put("one")
        self.put("two")
        calls = []

        def invoke(*args):
            calls.append(args[2]["job"])
            return {"status": "empty", "publishedPageIds": []}

        result = process_queue(self.config, invoke, clock=lambda: self.now)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(calls[0]["sourceJobIds"]), ["one", "two"])

    def test_batch_order_uses_created_time_before_filename(self):
        earlier = self.job("A", createdAt=(self.now - timedelta(seconds=300)).isoformat())
        later = self.job("B", createdAt=(self.now - timedelta(seconds=200)).isoformat())
        earlier["evidence"] = [{"id": "A-e", "kind": "user", "text": "A"}]
        later["evidence"] = [{"id": "B-e", "kind": "user", "text": "B"}]
        save_json(self.root / "queue/fff.json", earlier)
        save_json(self.root / "queue/000.json", later)
        calls = []
        process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"},
                      clock=lambda: self.now)
        self.assertEqual(calls[0]["sourceJobIds"], ["A", "B"])
        self.assertEqual([item["id"] for item in calls[0]["evidence"]], ["A-e", "B-e"])

    def test_different_projects_never_merge(self):
        self.put("one", project="growth")
        self.put("two", project="mobile")
        calls = []
        invoke = lambda *args: calls.append(args[2]["job"]) or {"status": "empty"}
        process_queue(self.config, invoke, limit=3, clock=lambda: self.now)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["projectId"], calls[1]["projectId"])

    def test_debounce_defers_new_local_turn(self):
        self.put("new", createdAt=self.now.isoformat())
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["processed"], 0)
        self.assertFalse(calls)
        self.assertEqual(result["deferred"], 1)

    def test_failure_sets_backoff_and_third_failure_isolated(self):
        self.put("bad", attempts=2)
        result = process_queue(self.config, lambda *args: {"status": "error"}, clock=lambda: self.now)
        self.assertEqual(result["processed"], 0)
        self.assertFalse((self.root / "queue/bad.json").exists())
        failed = load_json(self.root / "failed/bad.json")
        self.assertEqual(failed["attempts"], 3)
        self.assertIn("nextAttemptAt", failed)

    def test_interrupted_batch_retry_is_recoverable(self):
        self.put("a")
        self.put("b")
        calls = []
        def partial_retry(state, path, job, now, config):
            if path.name == "a.json":
                path.rename(Path(state) / "failed" / path.name)
                return
            raise KeyboardInterrupt()
        with patch("queue_finalization._retry_job", side_effect=partial_retry):
            with self.assertRaises(KeyboardInterrupt):
                process_queue(self.config, lambda *args: {"status": "error"}, clock=lambda: self.now)
        audit = next((self.root / "batches").glob("*.json"))
        self.assertEqual(load_json(audit)["status"], "failed")
        self.assertEqual(load_json(audit)["error"], "interrupted-partial")
        self.assertEqual(sorted(path.name for path in (self.root / "queue").glob("*.json")), ["b.json"])
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"},
                               clock=lambda: self.now + timedelta(seconds=301))
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(calls)
        self.assertEqual(load_json(self.root / "failed/b.json")["status"], "batch-failed")
        self.assertFalse((self.root / "queue/b.json").exists())

    def test_final_retry_result_replays_after_partial_terminal_source_move(self):
        """Persist ledger-bearing failure intent before moving the final source."""
        self.put("a", attempts=2)
        self.put("b", attempts=2)

        def interrupt_move(state, path, job, now, config):
            if path.name == "a.json":
                path.rename(Path(state) / "failed" / path.name)
                return
            raise KeyboardInterrupt()

        result = {"status": "error", "retryable": True, "error": "transient",
                  "ledgerContribution": {"version": 1, "records": []}}
        with patch("queue_finalization._retry_job", side_effect=interrupt_move):
            with self.assertRaises(KeyboardInterrupt):
                process_queue(self.config, lambda *args: result, clock=lambda: self.now)
        audit_path = next((self.root / "batches").glob("*.json"))
        audit = load_json(audit_path)
        self.assertEqual(audit["status"], "failure-finalize")
        self.assertEqual(audit["result"], result)

        exported, calls = [], []
        with patch("queue_finalization.export_result", side_effect=lambda cfg, job, value: exported.append(value) or value):
            process_queue(self.config,
                          lambda *args: calls.append(args) or {"status": "empty"},
                          clock=lambda: self.now + timedelta(seconds=301))
        self.assertEqual(calls, [])
        self.assertEqual(exported, [result])
        self.assertEqual(load_json(audit_path)["status"], "failed")
        self.assertFalse((self.root / "completed/a.json").exists())
        self.assertFalse((self.root / "completed/b.json").exists())

    def test_last_valid_ledger_error_survives_later_transport_failures(self):
        """A later transport error cannot erase the latest durable worker result."""
        self.put("mixed")
        accepted = {"status": "error", "retryable": True, "error": "phase1 warning",
                    "ledgerContribution": {"version": 1, "records": []}}
        calls, exported = [], []

        def invoke(*args):
            calls.append(args)
            if len(calls) == 1:
                return accepted
            raise RuntimeError("transport unavailable")

        process_queue(self.config, invoke, clock=lambda: self.now)
        process_queue(self.config, invoke, clock=lambda: self.now + timedelta(seconds=301))
        with patch("queue_finalization.export_result",
                   side_effect=lambda cfg, job, result: exported.append(result) or result):
            process_queue(self.config, invoke, clock=lambda: self.now + timedelta(seconds=901))

        audit = load_json(next((self.root / "batches").glob("*.json")))
        self.assertEqual(len(calls), 3)
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["result"], accepted)
        self.assertEqual(exported, [accepted])
        self.assertFalse((self.root / "completed/mixed.json").exists())

    def test_unknown_worker_status_is_retried(self):
        self.put("odd")
        process_queue(self.config, lambda *args: {"status": "unexpected"}, clock=lambda: self.now)
        self.assertTrue((self.root / "queue/odd.json").exists())

    def test_python_batch_envelope_replays_outside_ts_audit(self):
        job = self.job("job")
        merged = {**job, "id": "batch-replay", "sourceJobIds": ["job"], "sourceQueueFiles": ["job.json"]}
        save_json(self.root / "queue/job.json", job)
        save_json(self.root / "batches/batch-replay.json", {"batchId": "batch-replay", "status": "result-ready",
                                                               "queueFiles": ["job.json"], "job": merged,
                                                               "result": {"status": "empty"}})
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["recovered"], 1)
        self.assertFalse(calls)
        self.assertTrue((self.root / "batches/batch-replay.json").exists())
        self.assertFalse((self.root / "audit/batch-replay.json").exists())

    def test_result_ready_receipt_failure_retries_without_model(self):
        job = self.job("durable")
        merged = {**job, "id": "batch-durable", "sourceJobIds": ["durable"],
                  "sourceQueueFiles": ["durable.json"]}
        save_json(self.root / "queue/durable.json", job)
        save_json(self.root / "batches/batch-durable.json", {"batchId": "batch-durable",
                   "status": "result-ready", "queueFiles": ["durable.json"], "job": merged,
                   "result": {"status": "empty"}})
        calls = []
        with patch("queue_finalization.write_receipt", side_effect=OSError("temporary")):
            first = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
            second = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        audit = load_json(self.root / "batches/batch-durable.json")
        self.assertEqual(len(calls), 0)
        self.assertEqual(first["reason"], "finalize-error")
        self.assertEqual(second["reason"], "finalize-backoff")
        self.assertEqual(audit["result"]["status"], "empty")

    def test_result_ready_export_failure_retains_result_without_model(self):
        job = self.job("durable-export")
        merged = {**job, "id": "batch-export", "sourceJobIds": ["durable-export"],
                  "sourceQueueFiles": ["durable-export.json"]}
        save_json(self.root / "queue/durable-export.json", job)
        save_json(self.root / "batches/batch-export.json", {"batchId": "batch-export",
                   "status": "result-ready", "queueFiles": ["durable-export.json"], "job": merged,
                   "result": {"status": "empty"}})
        calls = []
        with patch("queue_finalization.export_result", side_effect=OSError("temporary")):
            process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
            process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(len(calls), 0)
        self.assertEqual(load_json(self.root / "batches/batch-export.json")["result"]["status"], "empty")

    def test_durable_finalization_recovers_after_three_failures_without_source(self):
        job = self.job("lost")
        merged = {**job, "id": "batch-lost", "sourceJobIds": ["lost"], "sourceQueueFiles": ["lost.json"]}
        save_json(self.root / "batches/batch-lost.json", {"batchId": "batch-lost", "status": "result-ready",
                   "queueFiles": ["lost.json"], "job": merged, "result": {"status": "empty"}})
        calls = []
        times = [self.now, self.now + timedelta(seconds=301), self.now + timedelta(seconds=902),
                 self.now + timedelta(seconds=2103)]
        with patch("queue_finalization.write_receipt", side_effect=[OSError(), OSError(), OSError(), None]):
            for current in times:
                process_queue(self.config, lambda *args: calls.append(args), clock=lambda current=current: current)
        audit = load_json(self.root / "batches/batch-lost.json")
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["result"]["status"], "empty")
        self.assertFalse(calls)

    def test_invoke_result_finalization_failure_never_retries_model(self):
        self.put("post-invoke")
        calls = []
        with patch("queue_finalization.write_receipt", side_effect=OSError("temporary")):
            first = process_queue(self.config, lambda *args: calls.append(args) or {"status": "empty"},
                                  clock=lambda: self.now)
            second = process_queue(self.config, lambda *args: calls.append(args) or {"status": "empty"},
                                   clock=lambda: self.now)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["reason"], "finalize-error")
        self.assertEqual(second["reason"], "finalize-backoff")

    def test_failed_batch_is_frozen_when_new_filename_sorts_first(self):
        save_json(self.root / "queue/z-old.json", self.job("old"))
        process_queue(self.config, lambda *args: {"status": "error"}, clock=lambda: self.now)
        later = lambda: self.now + timedelta(seconds=301)
        save_json(self.root / "queue/a-new.json", self.job("new"))
        calls = []
        process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"}, clock=later)
        self.assertEqual([job["sourceJobIds"] for job in calls], [["new"], ["old"]])

    def test_batch_keeps_all_evidence_under_source_size_limit(self):
        first, second = self.job("one"), self.job("two")
        first["evidence"] = [{"id": f"one-{i}", "kind": "user", "text": "x"} for i in range(20)]
        second["evidence"] = [{"id": f"two-{i}", "kind": "user", "text": "x"} for i in range(20)]
        save_json(self.root / "queue/one.json", first)
        save_json(self.root / "queue/two.json", second)
        calls = []
        process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"}, clock=lambda: self.now)
        self.assertEqual(len(calls[0]["evidence"]), 40)

    def test_oversize_first_job_is_isolated_before_model(self):
        self.put("huge")
        path = self.root / "queue/huge.json"
        job = load_json(path)
        job["evidence"] = [{"id": "x", "kind": "user", "text": "x" * 200_000}]
        save_json(path, job)
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["results"][0]["reason"], "oversize")
        self.assertTrue((self.root / "failed/huge.json").exists())

    def _v2_config(self):
        value = dict(self.config)
        value.update(machineId="a", publishEnabled=False)
        value["sharedWikiRoot"] = str(self.root / "shared")
        value["wikiRoot"] = str(self.root / "replica/current")
        value["exchange"] = {"protocolVersion": 2, "root": str(self.root / "exchange"),
                              "legacyImporterMachineId": "a", "participants": ["a", "b"]}
        return value

    def test_v2_batches_sync_and_pin_visible_basis(self):
        config = self._v2_config()
        self.put("first")
        statuses = [{"fullyVisibleRecordIds": [], "digest": "gen-1", "generationRoot": str(self.root / "g1")},
                    {"fullyVisibleRecordIds": ["first-publication"], "digest": "gen-2", "generationRoot": str(self.root / "g2")}]
        seen = []
        def sync_side_effect(_config, after_sync=None):
            status = statuses.pop(0)
            if after_sync:
                after_sync(status)
            return status
        with patch("replica.sync_replica", side_effect=sync_side_effect) as sync, patch("replica.read_records", side_effect=[[], [{"id": "first-publication", "payload": {"projectId": "growth"}}]]):
            invoke = lambda cfg, _kind, payload, _timeout: seen.append((cfg["wikiRoot"], payload["job"]["basisRecordIds"])) or {"status": "empty"}
            process_queue(config, invoke, limit=1, clock=lambda: self.now)
            self.put("second")
            process_queue(config, invoke, limit=1, clock=lambda: self.now)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(seen, [(str(self.root / "g1"), []),
                                (str(self.root / "g2"), ["first-publication"])])

    def test_v2_retry_reuses_frozen_basis_without_resync(self):
        config = self._v2_config()
        self.put("retry")
        generation = self.root / 'replica/generations' / ('a' * 64)
        save_json(generation / '.llmwiki/replica-response.json', {'pages': 0, 'conflicts': []})
        seal_generation(generation, generation.name)
        status = {"fullyVisibleRecordIds": ["record-a"], "digest": generation.name, "generationRoot": str(generation)}
        seen = []
        def sync_side_effect(_config, after_sync=None):
            if after_sync:
                after_sync(status)
            return status
        with patch("replica.sync_replica", side_effect=sync_side_effect) as sync, patch("replica.read_records", return_value=[{"id": "record-a", "payload": {"projectId": "growth"}}]):
            def invoke(cfg, _kind, payload, _timeout):
                seen.append((cfg["wikiRoot"], payload["job"]["basisRecordIds"]))
                return {"status": "error"} if len(seen) == 1 else {"status": "empty"}
            process_queue(config, invoke, clock=lambda: self.now)
            process_queue(config, invoke, clock=lambda: self.now + timedelta(seconds=301))
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(seen, [(str(generation), ["record-a"]), (str(generation), ["record-a"])])

    def test_v2_sync_failure_keeps_queue_without_budget_or_hot_retry(self):
        config = self._v2_config()
        self.put("sync-fail")
        status = {"fullyVisibleRecordIds": [], "digest": "gen-ok", "generationRoot": str(self.root / "g-ok")}
        calls = []
        sync_calls = []
        def sync_side_effect(_config, after_sync=None):
            if not sync_calls:
                sync_calls.append("failed")
                raise RuntimeError("cloud")
            if after_sync:
                after_sync(status)
            return status
        with patch("replica.sync_replica", side_effect=sync_side_effect) as sync, patch("replica.read_records", return_value=[]):
            first = process_queue(config, lambda *args: calls.append(args) or {"status": "empty"}, clock=lambda: self.now)
            second = process_queue(config, lambda *args: calls.append(args) or {"status": "empty"}, clock=lambda: self.now)
            self.assertFalse((self.root / "daily-budget.json").exists())
            third = process_queue(config, lambda *args: calls.append(args) or {"status": "empty"},
                                  clock=lambda: self.now + timedelta(seconds=301))
        self.assertEqual(first["reason"], "replica-sync")
        self.assertEqual(first["attempts"], 0)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(third["attempts"], 1)

    def test_v2_basis_is_project_scoped_and_zero_budget_does_not_invoke(self):
        config = self._v2_config()
        self.put("growth-job")
        status = {"fullyVisibleRecordIds": ["growth-record", "mobile-record"], "digest": "gen", "generationRoot": str(self.root / "g")}
        records = [{"id": "growth-record", "payload": {"projectId": "growth"}},
                   {"id": "mobile-record", "payload": {"projectId": "mobile"}}]
        config["maxDailyJobs"] = 0
        def sync_side_effect(_config, after_sync=None):
            if after_sync:
                after_sync(status)
            return status
        with patch("replica.sync_replica", side_effect=sync_side_effect), patch("replica.read_records", return_value=records):
            calls = []
            result = process_queue(config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertEqual(result["reason"], "daily-budget")
        self.assertFalse(calls)
        audit = next((self.root / "batches").glob("*.json"))
        self.assertEqual(load_json(audit)["basisRecordIds"], ["growth-record"])

    def test_locked_wake_returns_without_sleep_or_model(self):
        from wake import run_with_debounce
        self.put("locked")
        lock_path = self.root / "worker.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sleeps, calls = [], []
            result = run_with_debounce({**self.config, "eventDriven": {"enabled": True}},
                                       invoke=lambda *args: calls.append(args), clock=lambda: self.now,
                                       sleep_fn=sleeps.append)
        self.assertIn("lock-busy", result["reasons"])
        self.assertFalse(sleeps)
        self.assertFalse(calls)

    def test_budget_stops_before_model(self):
        self.put("one")
        save_json(self.root / "daily-budget.json", {"date": "2026-09-16", "used": 12})
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["results"][0]["reason"], "daily-budget")


if __name__ == "__main__":
    unittest.main()
