"""Host-injected messages are evidence about the run, never the user's own words.

Background task notifications, automation heartbeats, page events and
environment context arrive in transcripts with the user role. Treated as user
evidence, a planner anchors decisions to them and review then rejects the batch.
A message made only of such blocks becomes artifact evidence; anything with the
user's own text stays user evidence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import capture

NOTIFICATION = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n</task-notification>"
HEARTBEAT = "<heartbeat>\n<automation_id>daily</automation_id>\n<instructions>check</instructions>\n</heartbeat>"
OPEN_PAGE = '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'
ANSWER = '<send_user_message_question_reply>[{"answer":"keep the weekly cadence"}]</send_user_message_question_reply>'


class InjectedEvidenceTests(unittest.TestCase):
    """Drive the real capture path for both hosts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.event = {"session_id": "session-1", "turn_id": "turn-2", "cwd": str(self.root)}
        self.record = {"projectId": "growth", "createdAt": "2026-10-03T00:00:00Z"}
        self.config = {"stateDir": str(self.root / "state"), "env": {"CODEX_HOME": str(self.root)}}

    def host_kinds(self, texts):
        self.event["conversation_evidence"] = [{"id": f"u{index}", "kind": "user", "text": text}
                                               for index, text in enumerate(texts)]
        return [item["kind"] for item in capture.capture_evidence(self.event, self.record, self.config)]

    def codex_kinds(self, texts):
        def message(ordinal, role, text):
            return {"ordinal": ordinal, "type": "response_item", "timestamp": "2026-10-03T00:00:00Z",
                    "payload": {"type": "message", "id": f"m-{ordinal}", "role": role,
                                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-2"}}}
        rows = [{"ordinal": 0, "type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.root)}},
                {"ordinal": 1, "type": "turn_context", "payload": {"turn_id": "turn-2", "cwd": str(self.root)}}]
        rows += [message(2 + index, "user", text) for index, text in enumerate(texts)]
        rows += [message(2 + len(texts), "assistant", "done"),
                 {"ordinal": 3 + len(texts), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2"}}]
        path = self.root / "sessions" / "rollout.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.event["transcript_path"] = str(path)
        self.event["last_assistant_message"] = "done"
        return [item["kind"] for item in capture.capture_evidence(self.event, self.record, self.config)]

    def test_codex_notification_is_artifact_while_the_prompt_stays_user(self):
        """Given a notification and a real prompt in one turn, Then only the prompt is user evidence."""
        self.assertEqual(self.codex_kinds([NOTIFICATION, "keep the weekly cadence"]), ["artifact", "user", "assistant"])

    def test_host_pure_injected_messages_are_artifacts(self):
        """Given heartbeat, page event and environment blocks alone, Then none is user evidence."""
        environment = "<environment_context>\n<current_date>2026-10-03</current_date>\n</environment_context>"
        self.assertEqual(self.host_kinds([HEARTBEAT, OPEN_PAGE, environment]), ["artifact", "artifact", "artifact"])

    def test_user_answers_and_mixed_messages_stay_user(self):
        """A question reply or a block followed by the user's own text keeps user authority."""
        self.assertEqual(self.host_kinds([ANSWER, NOTIFICATION + "\nplease also archive the report"]), ["user", "user"])

    def test_notification_only_turn_has_no_user_evidence(self):
        """Given a turn triggered only by a notification, Then nothing can be cited as the user's decision."""
        self.assertNotIn("user", self.host_kinds([NOTIFICATION]))


if __name__ == "__main__":
    unittest.main()
