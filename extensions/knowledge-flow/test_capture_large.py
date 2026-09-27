"""Exercise long native turns through real JSONL files and the public capture path.

Tool output volume must not discard a turn's identity or visible evidence. These
checks also retain fail-closed session, ordering and incomplete-input boundaries.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture
import capture_transcript


def row(ordinal, kind, payload):
    """Build the native ordinal envelope without synthetic capture results."""
    return {"ordinal": ordinal, "type": kind, "payload": payload}


def message(ordinal, role, text):
    """Associate visible text with the target native turn."""
    return row(ordinal, "response_item", {"type": "message", "role": role,
        "content": [{"type": "text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"turn_id": "target"}})


class LongTurnCaptureTests(unittest.TestCase):
    """A complete long turn stays capturable without exposing its tool outputs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "sessions/long.jsonl"
        self.path.parent.mkdir()
        self.event = {"session_id": "session", "turn_id": "target", "cwd": str(self.root),
                      "hook_event_name": "Stop", "transcript_path": str(self.path)}
        self.record = {"projectId": "project", "reason": "owned-repository"}
        self.config = {"env": {"CODEX_HOME": str(self.root)}}
        self.rows = [row(0, "session_meta", {"id": "session"}),
                     row(1, "turn_context", {"turn_id": "target", "cwd": str(self.root)}),
                     message(2, "user", "采用成熟窗口统计"),
                     row(3, "response_item", {"type": "function_call_output", "output": "x" * 2000}),
                     message(4, "assistant", "按成熟窗口统计并保留来源"),
                     row(5, "event_msg", {"type": "task_complete", "turn_id": "target"})]

    def capture(self):
        """Read the fixture through the same bounded scanner as native intake."""
        self.path.write_text("\n".join(json.dumps(item) for item in self.rows) + "\n")
        return capture.capture_evidence_result(self.event, self.record, self.config)

    def test_long_turn_retains_its_start_across_more_than_32_mb_of_tools(self):
        """Given a real long transcript, capture all visible text from its first context."""
        tools = [row(index, "response_item", {"type": "function_call_output", "output": "x" * 1_000_000})
                 for index in range(3, 37)]
        tools[0]["payload"]["output"] = "x" * 9_000_000
        self.rows = self.rows[:3] + tools + [message(37, "assistant", "按成熟窗口统计并保留来源"),
            row(38, "event_msg", {"type": "task_complete", "turn_id": "target"})]
        self.path.write_text("\n".join(json.dumps(item) for item in self.rows) + "\n")
        result = capture.capture_evidence_result(self.event, self.record, self.config)
        self.assertEqual("ok", result["status"])
        self.assertEqual(["采用成熟窗口统计", "按成熟窗口统计并保留来源"],
                         [item["text"] for item in result["evidence"]])

    def test_long_turn_still_requires_matching_session_and_context(self):
        for target, field, foreign in [(0, "id", "other"), (1, "cwd", str(self.root / "other"))]:
            with self.subTest(field=field):
                original = self.rows[target]["payload"][field]
                self.rows[target]["payload"][field] = foreign
                self.assertEqual("unavailable", self.capture()["status"])
                self.rows[target]["payload"][field] = original

    def test_incomplete_hidden_record_is_not_silently_discarded(self):
        self.rows[3]["complete"] = False
        result = self.capture()
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("transcript-incomplete", result["reason"])

    def test_ordinal_regression_in_hidden_output_is_rejected(self):
        self.rows[3]["ordinal"] = 1
        self.assertEqual("transcript-ordinal-invalid", self.capture()["reason"])

    def test_partial_final_record_is_rejected(self):
        self.capture()
        with self.path.open("a") as stream:
            stream.write('{"ordinal":6')
        result = capture.capture_evidence_result(self.event, self.record, self.config)
        self.assertEqual("transcript-invalid-jsonl", result["reason"])

    def test_visible_record_and_retained_evidence_limits_still_apply(self):
        self.rows[2]["payload"]["content"][0]["text"] = "x" * 900
        with patch.object(capture_transcript, "MAX_TRANSCRIPT_BYTES", 800):
            self.assertEqual("transcript-line-too-large", self.capture()["reason"])
        with patch.object(capture_transcript, "MAX_NAMED_SCAN_BYTES", 800):
            self.assertEqual("transcript-evidence-limit-exceeded", self.capture()["reason"])

    def test_large_tool_output_does_not_depend_on_total_file_size(self):
        self.rows[3]["payload"]["output"] = "x" * 9_000_000
        result = self.capture()
        self.assertLess(self.path.stat().st_size, capture_transcript.MAX_NAMED_SCAN_BYTES)
        self.assertEqual("ok", result["status"])
        self.assertEqual(2, len(result["evidence"]))

    def test_scan_byte_and_time_limits_fail_visibly(self):
        with patch.object(capture_transcript, "MAX_CAPTURE_SCAN_BYTES", 2000):
            self.assertEqual("transcript-scan-limit-exceeded", self.capture()["reason"])
        with patch.object(capture_transcript, "monotonic", side_effect=[0, 5]):
            self.assertEqual("transcript-scan-timeout", self.capture()["reason"])


if __name__ == "__main__":
    unittest.main()
