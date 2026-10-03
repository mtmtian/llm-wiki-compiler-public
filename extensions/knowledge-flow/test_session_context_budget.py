"""A long Chinese session must keep capturing turns.

The session evidence window used to be counted in characters (40,000) while jobs are limited in
UTF-8 bytes (120,000). Forty thousand CJK characters plus per-item metadata exceed a whole job, so
once a long session filled its window every later turn was rejected as JobTooLarge and its
knowledge was never captured. The window is now measured in the same bytes as the job limit.
"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import hooks
from common import load_json
from queue_wire import job_bytes
from session_state import MAX_EVIDENCE_BYTES, commit_batch, load_session

# Shaped like the observed failure: 140 earlier assistant replies of about 300 CJK characters each.
EARLIER_TURNS = 140
REPLY = "部署完成后核对申报版本与检查结果，记录回测与待办。" * 12


class SessionContextBudgetTests(unittest.TestCase):
    """Given/When/Then checks for the session evidence window against the job byte limit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "projects": {"growth": {"pages": []}}, "maxDailyJobs": 20, "maxJobBytes": 120_000,
                       "sessionConsolidation": {"enabled": True}}

    def tearDown(self):
        self.temp.cleanup()

    def job(self, name, text):
        return {"id": name, "projectId": "growth", "sessionId": "session-1", "prompt": text,
                "createdAt": self.now.isoformat(), "evidence": [{"id": name, "kind": "assistant", "text": text}]}

    def fill_window(self):
        for index in range(EARLIER_TURNS):
            commit_batch(self.config, self.job(f"earlier-{index}", REPLY), {"status": "needs_review"})

    def test_window_is_bounded_in_job_bytes(self):
        """Given a long Chinese session, When its window is saved, Then it stays within the byte budget."""
        self.fill_window()
        evidence = load_session(self.config, "growth", "session-1")["evidence"]
        self.assertTrue(evidence)
        self.assertLessEqual(sum(job_bytes(item) for item in evidence), MAX_EVIDENCE_BYTES)

    def test_next_turn_of_a_long_session_is_queued(self):
        """Given a long Chinese session, When a short decision turn arrives, Then it is queued, not rejected."""
        self.fill_window()
        queued = self.root / "queue" / "next.json"
        hooks.enqueue_job(self.job("next", "决定采用方案 A，以后按字节计算会话窗口。"), queued, self.config)
        self.assertTrue(queued.exists())
        self.assertFalse((self.root / "capture-errors" / "next.json").exists())
        self.assertLessEqual(job_bytes(load_json(queued)), self.config["maxJobBytes"])


if __name__ == "__main__":
    unittest.main()
