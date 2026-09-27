"""Behavioral tests for bounded native Codex evidence capture."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import capture
from common import digest, save_json


class CaptureTests(unittest.TestCase):
    """Keep transcript, association, and evidence-quality guarantees explicit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.event = {"session_id": "session-1", "turn_id": "turn-2", "cwd": str(self.root),
                      "prompt": "分析 Acme PC 模拟实验的转化表现", "last_assistant_message": "已完成"}
        self.record = {"projectId": "growth", "reason": "business-workspace", "createdAt": "2026-09-16T00:00:00Z"}
        self.config = {"stateDir": str(self.root / "state"), "env": {"CODEX_HOME": str(self.root)}}

    def tearDown(self):
        self.temp.cleanup()

    def write_rollout(self, rows):
        path = self.sessions / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.event["transcript_path"] = str(path)
        return path

    def write_large_rollout(self, rows, huge_current=False):
        path = self.sessions / "large-rollout.jsonl"
        head = self.row(0, "session_meta", {"session_id": "session-1", "cwd": str(self.root)})
        with path.open("wb") as stream:
            stream.write((json.dumps(head) + "\n").encode())
            if huge_current:
                context = self.row(1, "turn_context", {"turn_id": "turn-2", "cwd": str(self.root)})
                stream.write((json.dumps(context) + "\n").encode())
                huge = self.message(2, "user", "turn-2", "x" * capture.MAX_TRANSCRIPT_BYTES)
                stream.write((json.dumps(huge) + "\n").encode())
            else:
                filler = [{"ordinal": index, "type": "world_state", "payload": {"state": "x" * 12000}}
                          for index in range(1, 701)]
                stream.write(("\n".join(json.dumps(row) for row in filler) + "\n").encode())
                shifted = [{**row, "ordinal": row["ordinal"] + 700} for row in rows[1:]]
                stream.write(("\n".join(json.dumps(row) for row in shifted) + "\n").encode())
        self.event["transcript_path"] = str(path)
        return path

    def row(self, ordinal, kind, payload):
        return {"ordinal": ordinal, "type": kind, "timestamp": "2026-09-16T00:00:00Z", "payload": payload}

    def message(self, ordinal, role, turn, text, item_id=None):
        return self.row(ordinal, "response_item", {"type": "message", "id": item_id or f"m-{ordinal}",
            "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}})

    def valid_rows(self):
        return [self.row(0, "session_meta", {"session_id": "session-1", "cwd": str(self.root)}),
                self.row(1, "event_msg", {"type": "task_started", "turn_id": "turn-2"}),
                self.row(2, "turn_context", {"turn_id": "turn-2", "cwd": str(self.root)}),
                self.message(3, "user", "turn-2", self.event["prompt"]),
                self.message(4, "assistant", "turn-2", "模拟实验转化表现应按相同观察窗口拆分"),
                self.row(5, "event_msg", {"type": "task_complete", "turn_id": "turn-2"})]

    def test_native_turn_without_decision_keyword_is_substantive(self):
        self.write_rollout(self.valid_rows())
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])
        self.assertTrue(capture.has_substantive_evidence(evidence))
        self.assertEqual(evidence[0]["sha256"], __import__("common").digest(self.event["prompt"]))

    def test_visible_messages_exclude_developer_reasoning_and_tool(self):
        rows = [self.valid_rows()[0], self.message(1, "developer", "turn-2", "hidden policy"),
                self.valid_rows()[2], self.row(3, "response_item", {"type": "reasoning", "id": "r", "summary": []}),
                self.row(4, "response_item", {"type": "function_call_output", "output": "hidden"}),
                self.message(5, "user", "turn-2", self.event["prompt"]),
                self.message(6, "assistant", "turn-2", "模拟实验转化表现应按相同观察窗口拆分"),
                self.row(7, "event_msg", {"type": "task_complete", "turn_id": "turn-2"})]
        self.write_rollout(rows)
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])
        self.assertNotIn("hidden", " ".join(item["text"] for item in evidence))

    def test_wrong_session_or_cwd_fails_closed(self):
        rows = self.valid_rows()
        rows[0]["payload"]["session_id"] = "other"
        self.write_rollout(rows)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])
        rows[0]["payload"]["session_id"] = "session-1"
        rows[2]["payload"]["cwd"] = str(self.root / "elsewhere")
        self.write_rollout(rows)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_workspace_change_uses_current_turn_directory(self):
        """Given a moved session, capture its completed turn in the bound workspace."""
        rows = self.valid_rows()
        rows[0]["payload"]["cwd"] = str(self.root / "original-workspace")
        self.write_rollout(rows)
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["text"] for item in evidence],
                         [self.event["prompt"], "模拟实验转化表现应按相同观察窗口拆分"])
        self.assertTrue(all("/turn/turn-2/" in item["locator"] for item in evidence))

    def test_workspace_change_still_requires_matching_session(self):
        """A matching turn directory must never authorize another session's text."""
        rows = self.valid_rows()
        rows[0]["payload"].update(session_id="another-session", cwd=str(self.root / "original"))
        self.write_rollout(rows)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_session_directory_cannot_replace_missing_turn_directory(self):
        """A matching session directory cannot authorize an unbound current turn."""
        rows = self.valid_rows()
        rows[2]["payload"].pop("cwd")
        self.write_rollout(rows)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_partial_jsonl_never_claims_complete_transcript(self):
        path = self.sessions / "partial.jsonl"
        path.write_text('{"ordinal":0,"type":"session_meta"}\n{"ordinal":1', encoding="utf-8")
        self.event["transcript_path"] = str(path)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_large_history_uses_metadata_and_bounded_tail(self):
        rows = self.valid_rows()
        self.write_large_rollout(rows)
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])

    def test_exact_filename_is_recovered_from_configured_archive_root(self):
        path = self.write_rollout(self.valid_rows())
        archive = self.root / "archived_sessions"
        archive.mkdir()
        archived = archive / path.name
        path.rename(archived)
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])

    def test_archive_root_not_declared_is_rejected(self):
        path = self.write_rollout(self.valid_rows())
        archive = self.root / "archived_sessions"
        archive.mkdir()
        path.rename(archive / path.name)
        self.config["env"] = {"CODEX_HOME": str(self.root / "other-home")}
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_archive_symlink_cannot_escape_the_trusted_root(self):
        original = self.write_rollout(self.valid_rows())
        outside = self.root / "outside.jsonl"
        original.rename(outside)
        archive = self.root / "archived_sessions"
        archive.mkdir()
        (archive / original.name).symlink_to(outside)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_two_native_archive_locations_are_ambiguous(self):
        original = self.write_rollout(self.valid_rows())
        text = original.read_text()
        original.unlink()
        self.event["transcript_path"] = str(self.sessions / "2026/09/21" / original.name)
        archive = self.root / "archived_sessions"
        dated = archive / "2026/09/21" / original.name
        dated.parent.mkdir(parents=True)
        dated.write_text(text)
        (archive / original.name).write_text(text)
        result = capture.capture_evidence_result(self.event, self.record, self.config)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "archived-filename-ambiguous")

    def test_oversized_current_turn_without_tail_context_is_rejected(self):
        self.write_large_rollout(self.valid_rows(), huge_current=True)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_stop_accepts_verified_final_answer_before_task_complete_event(self):
        rows = self.valid_rows()[:-1]
        self.event["hook_event_name"] = "Stop"
        self.event["last_assistant_message"] = "模拟实验转化表现应按相同观察窗口拆分"
        self.write_rollout(rows)
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])

    def test_pure_confirmation_is_not_substantive(self):
        self.event["prompt"] = "同意"
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["kind"] for item in evidence], ["user", "assistant"])
        self.assertFalse(capture.has_substantive_evidence(evidence))

    def test_confirmation_with_new_assistant_analysis_is_substantive(self):
        self.event["prompt"] = "继续"
        self.record["prompt"] = "继续"
        self.event["last_assistant_message"] = "这里补充分析模拟实验中观察窗口与转化表现变化的关系"
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertTrue(capture.has_substantive_evidence(evidence))

    def test_transcript_outside_configured_root_is_ignored(self):
        path = self.root / "outside.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        self.event["transcript_path"] = str(path)
        self.assertEqual(capture.capture_evidence(self.event, self.record, self.config), [])

    def test_capture_status_distinguishes_missing_transcript_from_empty_prompt(self):
        self.event["transcript_path"] = str(self.sessions / "missing.jsonl")
        unavailable = capture.capture_evidence_result(self.event, self.record, self.config)
        self.assertEqual(unavailable["status"], "unavailable")
        self.event.pop("transcript_path")
        self.record.pop("prompt", None)
        self.event.pop("prompt")
        empty = capture.capture_evidence_result(self.event, self.record, self.config)
        self.assertEqual(empty["status"], "empty")

    def test_host_evidence_is_preserved_and_assistant_is_claim_only(self):
        self.event["conversation_evidence"] = [
            {"id": "u", "kind": "user", "text": "请比较两组投放"},
            {"id": "a", "kind": "assistant", "text": "已完成比较"},
        ]
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertEqual([item["id"] for item in evidence], ["codex-" + __import__("common").digest("u")[:24],
                                                               "codex-" + __import__("common").digest("a")[:24]])
        self.assertEqual(evidence[1]["evidenceStatus"], "assistant-claim")

    def test_routed_confirmation_can_include_small_prior_exchange(self):
        rows = self.valid_rows()
        prior_context = self.row(2, "turn_context", {"turn_id": "turn-1", "cwd": str(self.root)})
        prior_user = self.message(3, "user", "turn-1", "确定按成熟窗口拆分")
        prior_assistant = self.message(4, "assistant", "turn-1", "可以")
        current = [self.row(5, "turn_context", {"turn_id": "turn-2", "cwd": str(self.root)}),
                   self.message(6, "user", "turn-2", "同意"), self.message(7, "assistant", "turn-2", "收到"),
                   self.row(8, "event_msg", {"type": "task_complete", "turn_id": "turn-2"})]
        self.event["prompt"] = "同意"
        self.record["reason"] = "business-continuation"
        save_json(self.root / "state" / "turns" / (digest("session-1:turn-1") + ".json"),
                  {"projectId": "growth"})
        self.write_rollout([rows[0], prior_context, prior_user, prior_assistant, *current])
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertTrue(any(item["historical"] for item in evidence))
        self.assertEqual(evidence[-2]["text"], "同意")
        self.assertTrue(capture.has_substantive_evidence(evidence))
        thanks = [dict(item) for item in evidence]
        thanks[-2]["text"] = "谢谢"
        self.assertFalse(capture.has_substantive_evidence(thanks))

    def test_routed_confirmation_rejects_cross_project_prior_exchange(self):
        self.test_routed_confirmation_can_include_small_prior_exchange()
        save_json(self.root / "state" / "turns" / (digest("session-1:turn-1") + ".json"),
                  {"projectId": "other"})
        self.event["prompt"] = "同意"
        self.record["reason"] = "business-continuation"
        self.event["transcript_path"] = str(self.sessions / "rollout.jsonl")
        evidence = capture.capture_evidence(self.event, self.record, self.config)
        self.assertFalse(any(item["historical"] for item in evidence))


if __name__ == "__main__":
    unittest.main()
