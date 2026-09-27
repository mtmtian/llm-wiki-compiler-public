"""Behavioral tests for automatic transcript race recovery."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture_retry
import hooks
import wake
from common import load_json


class CaptureRetryTests(unittest.TestCase):
    """Verify pending retries preserve identity and never bypass queue limits."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"enabled": True, "stateDir": str(self.root / "state"), "maxQueuedJobs": 2,
                       "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放"], "pages": []}},
                       "env": {"CODEX_HOME": str(self.root)}, "eventDriven": {"enabled": True}}
        self.event = {"session_id": "session", "turn_id": "turn", "cwd": str(self.root),
                      "hook_event_name": "UserPromptSubmit", "prompt": "ProductX投放采用新口径",
                      "transcript_path": str(self.root / "sessions" / "rollout.jsonl")}
        (self.root / "sessions").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _row(self, ordinal, kind, payload):
        return {"ordinal": ordinal, "type": kind, "timestamp": "2026-09-21T00:00:00Z", "payload": payload}

    def _message(self, ordinal, role, text):
        return self._row(ordinal, "response_item", {"type": "message", "id": f"m-{ordinal}", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "turn"}})

    def _complete_rollout(self):
        rows = [self._row(0, "session_meta", {"session_id": "session"}),
                self._row(1, "turn_context", {"turn_id": "turn", "cwd": str(self.root)}),
                self._message(2, "user", self.event["prompt"]),
                self._message(3, "assistant", "以后统一按新口径复盘"),
                self._row(4, "event_msg", {"type": "task_complete", "turn_id": "turn"})]
        path = Path(self.event["transcript_path"])
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_stop_pending_then_wake_replays_appended_complete_transcript(self):
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": "以后统一按新口径复盘"}
        hooks.handle(stop, self.config)
        identifier = hooks.event_path(self.config, stop).stem
        self.assertTrue((self.root / "state/capture-pending" / f"{identifier}.json").exists())
        self._complete_rollout()
        pending = load_json(self.root / "state/capture-pending" / f"{identifier}.json")
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        report = capture_retry.process_capture_retries(self.config, due)
        self.assertEqual(report["recovered"], 1)
        self.assertTrue((self.root / "state/queue" / f"{identifier}.json").exists())
        self.assertFalse((self.root / "state/capture-pending" / f"{identifier}.json").exists())

    def test_queue_full_keeps_pending_until_capacity_returns(self):
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop"}
        hooks.handle(stop, self.config)
        self._complete_rollout()
        identifier = hooks.event_path(self.config, stop).stem
        self.config["maxQueuedJobs"] = 0
        pending = load_json(self.root / "state/capture-pending" / f"{identifier}.json")
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        report = capture_retry.process_capture_retries(self.config, due)
        self.assertEqual(report["pending"], 1)
        self.assertTrue((self.root / "state/capture-pending" / f"{identifier}.json").exists())
        self.config["maxQueuedJobs"] = 2
        pending = load_json(self.root / "state/capture-pending" / f"{identifier}.json")
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        self.assertEqual(capture_retry.process_capture_retries(self.config, due)["recovered"], 1)

    def test_duplicate_stop_and_background_retry_keep_original_artifact_and_route(self):
        """Given pending evidence, a changed file and route cannot change the recovered job."""
        artifact = self.root / "decision.md"
        artifact.write_text("original evidence")
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop",
                "last_assistant_message": f"完成 [证据]({artifact})"}
        hooks.handle(stop, self.config)
        identifier = hooks.event_path(self.config, stop).stem
        pending_path = self.root / "state/capture-pending" / f"{identifier}.json"
        pending = load_json(pending_path)
        artifact.write_text("changed after Stop")
        self._complete_rollout()
        hooks.handle({**stop, "prompt": "Other project changed route"}, self.config)
        self.assertEqual(load_json(pending_path), pending)
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        self.assertEqual(capture_retry.process_capture_retries(self.config, due)["recovered"], 1)
        queued = load_json(self.root / "state/queue" / f"{identifier}.json")
        self.assertEqual(queued["projectId"], "growth")
        self.assertEqual(queued["prompt"], self.event["prompt"])
        self.assertEqual([item["text"] for item in queued["evidence"] if item["kind"] == "artifact"],
                         ["original evidence"])

    def test_pending_uses_prompt_transcript_when_stop_omits_path(self):
        hooks.handle(self.event, self.config)
        stop = {key: value for key, value in self.event.items() if key != "transcript_path"}
        stop["hook_event_name"] = "Stop"
        hooks.handle(stop, self.config)
        identifier = hooks.event_path(self.config, stop).stem
        pending_path = self.root / "state/capture-pending" / f"{identifier}.json"
        pending = load_json(pending_path)
        self.assertEqual(pending["event"]["transcript_path"], self.event["transcript_path"])
        self._complete_rollout()
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        self.assertEqual(capture_retry.process_capture_retries(self.config, due)["recovered"], 1)

    def test_wrong_cwd_does_not_replay_native_text(self):
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop"}
        hooks.handle(stop, self.config)
        self._complete_rollout()
        rows = json.loads((self.root / "sessions/rollout.jsonl").read_text().splitlines()[1])
        rows["payload"]["cwd"] = str(self.root / "other")
        lines = (self.root / "sessions/rollout.jsonl").read_text().splitlines()
        lines[1] = json.dumps(rows)
        (self.root / "sessions/rollout.jsonl").write_text("\n".join(lines) + "\n")
        identifier = hooks.event_path(self.config, stop).stem
        pending = load_json(self.root / "state/capture-pending" / f"{identifier}.json")
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        self.assertEqual(capture_retry.process_capture_retries(self.config, due)["pending"], 1)
        self.assertFalse((self.root / "state/queue" / f"{identifier}.json").exists())

    def test_wake_replays_pending_capture_before_queue_drain(self):
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop"}
        hooks.handle(stop, self.config)
        self._complete_rollout()
        identifier = hooks.event_path(self.config, stop).stem
        pending = load_json(self.root / "state/capture-pending" / f"{identifier}.json")
        due = dt.datetime.fromisoformat(pending["nextAttemptAt"]) + dt.timedelta(seconds=1)
        with patch.object(wake, "process_queue", return_value={"processed": 0, "attempts": 0,
                                                                  "reason": "reconcile-only"}):
            result = wake.run_once(self.config, clock=lambda: due)
        self.assertEqual(result["drain"]["captureRecovery"]["recovered"], 1)
        self.assertEqual(result["counters"]["capture-pending"], 0)

    def test_existing_worker_lock_defers_without_touching_pending(self):
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop"}
        hooks.handle(stop, self.config)
        lock_path = self.root / "state/worker.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = capture_retry.process_capture_retries(self.config)
            self.assertEqual(report["reason"], "lock-busy")
        identifier = hooks.event_path(self.config, stop).stem
        self.assertTrue((self.root / "state/capture-pending" / f"{identifier}.json").exists())


if __name__ == "__main__":
    unittest.main()
