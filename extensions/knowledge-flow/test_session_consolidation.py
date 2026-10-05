"""Behavior checks for durable per-session consolidation windows."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from common import load_json, save_json
from queue_wire import job_bytes
import hooks
import session_state
from queue_schedule import due_at
from queue_worker import process_queue
from session_state import (commit_batch, context_for_job, load_session, persist_queued_job,
                           state_path)
from session_state import record_pending


class SessionConsolidationTests(unittest.TestCase):
    """Given/When/Then checks for cross-turn state and frozen batches."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 17, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "projects": {"growth": {"pages": []}}, "maxDailyJobs": 20,
                       "sessionConsolidation": {"enabled": True}}

    def tearDown(self):
        self.temp.cleanup()

    def job(self, name, created=None, **extra):
        value = {"id": name, "projectId": "growth", "sessionId": "session-1",
                 "prompt": name, "evidence": [{"id": name, "kind": "user", "text": name}],
                 "sessionContext": {"version": 1, "revision": 0, "summary": "",
                                    "topicPageIds": [], "evidence": []}}
        if created:
            value["createdAt"] = created.isoformat()
        value.update(extra)
        return value

    def put(self, job):
        save_json(self.root / "queue" / (job["id"] + ".json"), job)

    def test_quiet_window_and_max_wait(self):
        """Given active turns, When quiet expires or max wait hits, Then batch is due."""
        first = self.now
        self.put(self.job("first", first))
        self.assertGreater(due_at(load_json(self.root / "queue/first.json"), self.now, self.config), self.now)
        self.assertEqual(due_at(load_json(self.root / "queue/first.json"), self.now + timedelta(seconds=301), self.config),
                         self.now + timedelta(seconds=300))
        state = load_session(self.config, "growth", "session-1")
        state["pending"] = [{"id": "first", "queuedAt": first.isoformat()}]
        state["firstQueuedAt"] = (self.now - timedelta(seconds=1801)).isoformat()
        state["lastQueuedAt"] = self.now.isoformat()
        save_json(state_path(self.config, "growth", "session-1"), state)
        self.assertLessEqual(due_at(self.job("first", first), self.now, self.config), self.now)

    def test_max_wait_is_thirty_minutes_for_a_continuous_session(self):
        """Given a continuously active session, When 30 minutes elapse, Then it must flush."""
        state = load_session(self.config, "growth", "session-1")
        state["firstQueuedAt"] = self.now.isoformat()
        state["lastQueuedAt"] = (self.now + timedelta(seconds=1_700)).isoformat()
        save_json(state_path(self.config, "growth", "session-1"), state)
        job = self.job("long", self.now)
        self.assertGreater(due_at(job, self.now + timedelta(seconds=1_799), self.config),
                           self.now + timedelta(seconds=1_799))
        self.assertLessEqual(due_at(job, self.now + timedelta(seconds=1_800), self.config),
                             self.now + timedelta(seconds=1_800))

    def test_explicit_consolidation_is_immediate(self):
        """Given a pending session, When consolidation is requested, Then it is immediately due."""
        job = self.job("request", self.now, requestConsolidation=True)
        self.put(job)
        self.assertEqual(due_at(job, self.now, self.config), self.now)

    def test_same_session_ignores_created_window_and_respects_bounds(self):
        """Given turns spread apart, When due, Then one ordered batch uses configured caps."""
        self.config.update(maxBatchJobs=2, maxBatchBytes=100_000)
        self.put(self.job("a", self.now - timedelta(hours=2)))
        self.put(self.job("b", self.now - timedelta(minutes=1)))
        self.put(self.job("c", self.now))
        calls = []
        process_queue(self.config, lambda *_args: calls.append(_args[2]["job"]) or {"status": "empty"},
                      clock=lambda: self.now)
        self.assertEqual(calls[0]["sourceJobIds"], ["a", "b"])
        self.assertTrue((self.root / "queue/c.json").exists())

    def test_marked_and_legacy_jobs_never_share_a_batch(self):
        """Given old and new contracts in one session, When drained, Then each keeps its own pipeline."""
        old = self.job("old-contract", self.now - timedelta(seconds=301))
        old.pop("sessionContext")
        new = self.job("new-contract", self.now - timedelta(seconds=301))
        self.put(old)
        self.put(new)
        calls = []
        process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"},
                      clock=lambda: self.now, limit=2)
        self.assertEqual({tuple(item["sourceJobIds"]) for item in calls},
                         {("old-contract",), ("new-contract",)})
        legacy = next(item for item in calls if item["sourceJobIds"] == ["old-contract"])
        current = next(item for item in calls if item["sourceJobIds"] == ["new-contract"])
        self.assertNotIn("sessionContext", legacy)
        self.assertIn("sessionContext", current)

    def test_batch_prompt_is_complete(self):
        """Given long consecutive prompts, When merged, Then no prompt text is silently truncated."""
        first = self.job("first", self.now - timedelta(seconds=301))
        second = self.job("second", self.now - timedelta(seconds=301))
        first["prompt"], second["prompt"] = "A" * 30_000, "B" * 30_000
        self.put(first)
        self.put(second)
        seen = []
        process_queue(self.config, lambda *args: seen.append(args[2]["job"]) or {"status": "empty"},
                      clock=lambda: self.now)
        self.assertIn("A" * 30_000, seen[0]["prompt"])
        self.assertIn("B" * 30_000, seen[0]["prompt"])

    def test_session_pipeline_timeout_has_safe_overhead(self):
        """Given a new session batch, When invoked, Then the subprocess gets a 1000 second bound."""
        self.put(self.job("timeout", self.now - timedelta(seconds=301)))
        seen = []
        process_queue(self.config, lambda *args: seen.append(args[3]) or {"status": "empty"},
                      clock=lambda: self.now)
        self.assertGreaterEqual(seen[0], 1_000)

    def test_batch_byte_limit_quarantines_single_oversize_session_job(self):
        """Given a batch cap below one source, When due, Then it is quarantined without model work."""
        self.config["maxBatchBytes"] = 1_000
        job = self.job("too-wide", self.now - timedelta(seconds=301))
        job["evidence"] = [{"id": "wide", "kind": "user", "text": "x" * 2_000}]
        self.put(job)
        calls = []
        result = process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(result["results"][0]["reason"], "oversize")
        self.assertTrue((self.root / "failed/too-wide.json").exists())

    def test_finalize_advances_checkpoint_once_and_keeps_evidence(self):
        """Given a successful empty result, When finalize is replayed, Then revision advances once."""
        self.put(self.job("one", self.now - timedelta(seconds=301)))
        invoke = lambda *_args: {"status": "empty"}
        process_queue(self.config, invoke, clock=lambda: self.now)
        first = load_session(self.config, "growth", "session-1")
        process_queue(self.config, invoke, clock=lambda: self.now)
        second = load_session(self.config, "growth", "session-1")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["evidence"][0]["text"], "one")

    def test_new_turn_is_not_added_to_frozen_batch(self):
        """Given a claimed batch, When a new turn arrives, Then its source stays out of frozen input."""
        self.put(self.job("old", self.now - timedelta(seconds=301)))
        seen = []
        def invoke(*args):
            seen.append(args[2]["job"])
            self.put(self.job("new", self.now))
            return {"status": "empty"}
        process_queue(self.config, invoke, clock=lambda: self.now)
        self.assertEqual(seen[0]["sourceJobIds"], ["old"])
        self.assertTrue((self.root / "queue/new.json").exists())

    def test_new_turn_during_model_run_is_pending_for_next_batch(self):
        """Given a frozen batch, When a new turn arrives during model work, Then checkpoint keeps it pending."""
        self.put(self.job("old", self.now - timedelta(seconds=301)))
        from session_state import record_pending
        seen = []
        def invoke(*args):
            seen.append(args[2]["job"])
            new = self.job("new", self.now)
            save_json(self.root / "queue/new.json", new)
            record_pending(self.config, new, self.now)
            return {"status": "empty"}
        process_queue(self.config, invoke, clock=lambda: self.now)
        state = load_session(self.config, "growth", "session-1")
        self.assertEqual(seen[0]["sourceJobIds"], ["old"])
        self.assertEqual([item["id"] for item in state["pending"]], ["new"])

    def test_finalize_interruption_replays_without_model_or_double_checkpoint(self):
        """Given finalize fails after model output, When retried, Then model and revision occur once."""
        self.put(self.job("interrupt", self.now - timedelta(seconds=301)))
        calls = []
        with patch("queue_finalization.write_receipt", side_effect=[OSError("temporary"), None]):
            first = process_queue(self.config, lambda *args: calls.append(args) or {"status": "empty"},
                                  clock=lambda: self.now)
            second = process_queue(self.config, lambda *args: calls.append(args) or {"status": "empty"},
                                   clock=lambda: self.now + timedelta(seconds=301))
        self.assertEqual((first["reason"], second["attempts"], len(calls)), ("finalize-error", 0, 1))
        self.assertEqual(load_session(self.config, "growth", "session-1")["revision"], 1)

    def test_state_isolated_by_project_and_memory_fallback_is_stable(self):
        """Given identical native ids, When projects commit, Then revisions and summaries stay isolated."""
        growth = self.job("growth", self.now - timedelta(seconds=301))
        app = {**growth, "id": "app", "projectId": "app"}
        commit_batch(self.config, growth, {"status": "empty", "sessionMemory": {"summary": "采用 A"}})
        commit_batch(self.config, app, {"status": "empty"})
        first = load_session(self.config, "growth", "session-1")
        second = load_session(self.config, "app", "session-1")
        self.assertEqual((first["revision"], first["summary"]), (1, "采用 A"))
        self.assertEqual((second["revision"], second["summary"]), (1, ""))
        self.assertEqual(context_for_job(self.config, "growth", "session-1")["summary"], "采用 A")

    def test_recent_evidence_is_bounded_without_losing_cursor(self):
        """Given oversized accumulated evidence, When committed, Then the cursor advances and window is bounded."""
        for index in range(3):
            item = self.job(str(index), self.now - timedelta(seconds=301))
            item["evidence"] = [{"id": str(index), "kind": "user", "text": "x" * 20_000}]
            commit_batch(self.config, item, {"status": "needs_review"})
        state = load_session(self.config, "growth", "session-1")
        self.assertEqual(state["revision"], 3)
        self.assertEqual(state["cursor"], ["0", "1", "2"])
        self.assertLessEqual(sum(job_bytes(item) for item in state["evidence"]), session_state.MAX_EVIDENCE_BYTES)

    def test_replayed_batch_cannot_overwrite_newer_memory(self):
        """Given a committed batch, When an old result replays, Then its summary is ignored."""
        job = self.job("stable", self.now)
        commit_batch(self.config, job, {"status": "empty", "sessionMemory": {"summary": "new"}})
        commit_batch(self.config, job, {"status": "empty", "sessionMemory": {"summary": "old"}})
        self.assertEqual(load_session(self.config, "growth", "session-1")["summary"], "new")

    def test_corrupt_state_rejects_new_context(self):
        """Given corrupt durable state, When context is requested, Then no empty replacement is used."""
        path = state_path(self.config, "growth", "session-1")
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            context_for_job(self.config, "growth", "session-1")

    def test_invalid_state_schema_never_resets_to_empty(self):
        """Given critical state fields with invalid types, When loaded, Then the state is rejected."""
        cases = ({"version": 2}, {"version": 1, "revision": "1"},
                 {"version": 1, "topicPageIds": "page"}, {"version": 1, "cursor": {}})
        for value in cases:
            with self.subTest(value=value):
                payload = {"version": 1, "projectId": "growth", "sessionId": "session-1",
                           "revision": 0, "summary": "old", "topicPageIds": [], "evidence": [],
                           "cursor": [], "pending": [], **value}
                path = state_path(self.config, "growth", "session-1")
                path.parent.mkdir(parents=True, exist_ok=True)
                save_json(path, payload)
                with self.assertRaises(ValueError):
                    load_session(self.config, "growth", "session-1")

    def test_corrupt_queue_is_quarantined_and_pending_is_released(self):
        """Given unreadable queued JSON, When drained, Then it moves intact to failed with a diagnostic."""
        broken = self.job("broken", self.now - timedelta(seconds=301), queueFile="broken.json")
        record_pending(self.config, broken, self.now - timedelta(seconds=301))
        (self.root / "queue").mkdir(parents=True, exist_ok=True)
        (self.root / "queue/broken.json").write_bytes(b"{not-json")
        calls = []
        process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual((self.root / "failed/broken.json").read_bytes(), b"{not-json")
        self.assertFalse(load_session(self.config, "growth", "session-1")["pending"])
        self.assertEqual(load_json(self.root / "last-error.json")["type"], "QueueSourceInvalid")

    def test_queue_record_without_id_is_quarantined_without_invented_identity(self):
        """Given a parseable queue object without id, When drained, Then it is preserved with explicit error metadata."""
        save_json(self.root / "queue/no-id.json", {"projectId": "growth", "sessionId": "session-1"})
        calls = []
        process_queue(self.config, lambda *args: calls.append(args), clock=lambda: self.now)
        self.assertFalse(calls)
        self.assertEqual(load_json(self.root / "failed/no-id.json"),
                         {"projectId": "growth", "sessionId": "session-1"})
        self.assertEqual(load_json(self.root / "failed/no-id.json.diagnostic.json")["reason"], "missing-id")

    def test_terminal_source_does_not_force_next_session_window(self):
        """Given a quarantined source, When a later turn arrives, Then its quiet window starts fresh."""
        failed = self.job("failed", self.now - timedelta(seconds=1_900), attempts=3)
        record_pending(self.config, failed, self.now - timedelta(seconds=1_900))
        self.put(failed)
        process_queue(self.config, lambda *_args: self.fail("terminal source must not invoke"),
                      clock=lambda: self.now)
        self.assertFalse(load_session(self.config, "growth", "session-1")["pending"])
        fresh = self.job("fresh", self.now)
        record_pending(self.config, fresh, self.now)
        self.assertGreater(due_at(fresh, self.now, self.config), self.now)
        self.assertTrue((self.root / "failed/failed.json").exists())

    def test_queue_write_crash_does_not_leave_inflight_pending(self):
        """Given queue persistence fails, When the hook crashes, Then no phantom session window remains."""
        job = self.job("inflight", self.now)
        queued = self.root / "queue/inflight.json"
        real_save = session_state.save_json

        def fail_queue(path, value):
            if Path(path) == queued:
                raise OSError("queue write interrupted")
            return real_save(path, value)

        with patch.object(session_state, "save_json", side_effect=fail_queue):
            with self.assertRaises(OSError):
                hooks.enqueue_job(job, queued, self.config)
        self.assertFalse(queued.exists())
        self.assertFalse(load_session(self.config, "growth", "session-1")["pending"])

    def test_startup_recovers_queue_written_before_checkpoint(self):
        """Given queue-first persistence is interrupted, When worker starts, Then it rebuilds pending."""
        job = self.job("startup-recovery", self.now)
        queued = self.root / "queue/startup-recovery.json"
        checkpoint = state_path(self.config, "growth", "session-1")
        real_save = session_state.save_json

        def fail_checkpoint(path, value):
            if Path(path) == checkpoint:
                raise OSError("checkpoint write interrupted")
            return real_save(path, value)

        with patch.object(session_state, "save_json", side_effect=fail_checkpoint):
            with self.assertRaises(OSError):
                persist_queued_job(self.config, job, queued)
        self.assertTrue(queued.exists())
        self.assertFalse(load_session(self.config, "growth", "session-1")["pending"])
        schedule = load_json(queued)["sessionSchedule"]
        due = datetime.fromisoformat(schedule["lastQueuedAt"]) + timedelta(seconds=301)
        calls = []
        process_queue(self.config, lambda *args: calls.append(args[2]["job"]) or {"status": "empty"},
                      clock=lambda: due)
        self.assertEqual(calls[0]["sourceJobIds"], ["startup-recovery"])
        self.assertEqual(load_session(self.config, "growth", "session-1")["revision"], 1)

    def test_startup_releases_pending_with_terminal_receipt(self):
        """Given a consumed source left a marker, When worker starts, Then it clears only that marker."""
        job = self.job("already-consumed", self.now)
        record_pending(self.config, job, self.now)
        save_json(self.root / "completed/already-consumed.json", {"status": "empty"})
        process_queue(self.config, lambda *_args: self.fail("terminal source must not invoke"),
                      clock=lambda: self.now)
        state = load_session(self.config, "growth", "session-1")
        self.assertEqual(state["pending"], [])
        self.assertEqual((state["revision"], state["cursor"]), (0, []))

    def test_startup_drops_dangling_marker_before_fresh_window(self):
        """Given an old queue marker with no source, When a fresh turn arrives, Then quiet starts at fresh."""
        lost = self.job("lost", self.now - timedelta(hours=12), queueFile="lost.json")
        record_pending(self.config, lost, self.now - timedelta(hours=12))
        process_queue(self.config, lambda *_args: self.fail("dangling source must not invoke"),
                      clock=lambda: self.now)
        self.assertFalse(load_session(self.config, "growth", "session-1")["pending"])
        fresh = self.job("fresh", self.now)
        fresh["queueFile"] = "fresh.json"
        save_json(self.root / "queue/fresh.json", fresh)
        record_pending(self.config, fresh, self.now)
        self.assertEqual(due_at(fresh, self.now, self.config), self.now + timedelta(seconds=300))

    def test_dynamic_repo_label_uses_verified_repository_name(self):
        """Given an unmapped verified repo, When intake builds a job, Then its label is readable."""
        event = {"session_id": "s", "turn_id": "t", "cwd": str(self.root),
                 "last_assistant_message": "done"}
        record = {"projectId": "repo-example-user-sample-repo", "repoIdentity": "example-user/sample-repo",
                  "prompt": "网页实现", "createdAt": self.now.isoformat()}
        captured = {"status": "ok", "evidence": []}
        job = hooks.prepare_job(event, record, {**self.config, "projects": {}}, captured)
        self.assertEqual(job["projectLabel"], "sample-repo")


if __name__ == "__main__":
    unittest.main()
