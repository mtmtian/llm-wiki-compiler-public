"""Fail-closed tests for the Alma pagination bridge."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("alma-session.py")
SPEC = importlib.util.spec_from_file_location("alma_session", MODULE_PATH)
assert SPEC and SPEC.loader
alma_session = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alma_session)


class AlmaSessionTests(unittest.TestCase):
    """Require complete envelopes and continuous cursors."""

    def page(self, **overrides):
        value = {"messages": [{"id": "m1"}], "hasMore": False, "nextOffset": 1,
                 "missingMessageIds": [], "sourceTextTruncated": False}
        value.update(overrides)
        return subprocess.CompletedProcess([], 0, json.dumps(value), "")

    def test_read_thread_follows_envelope_cursor(self):
        responses = [self.page(messages=[{"id": "m1"}], hasMore=True, nextOffset=1),
                     self.page(messages=[{"id": "m2"}], nextOffset=2)]
        with patch.object(alma_session.subprocess, "run", side_effect=responses) as run:
            self.assertEqual([item["id"] for item in alma_session.read_thread("thread")], ["m1", "m2"])
        self.assertIn("--offset", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[1].args[0][run.call_args_list[1].args[0].index("--offset") + 1], "1")

    def test_incomplete_metadata_fails_closed(self):
        for field, value in (("missingMessageIds", ["m2"]), ("sourceTextTruncated", True)):
            with self.subTest(field=field), patch.object(alma_session.subprocess, "run", return_value=self.page(**{field: value})):
                with self.assertRaises(ValueError):
                    alma_session.read_thread("thread")

    def test_discontinuous_cursor_fails_closed(self):
        with patch.object(alma_session.subprocess, "run", return_value=self.page(nextOffset=4)):
            with self.assertRaisesRegex(ValueError, "discontinuous"):
                alma_session.read_thread("thread")


if __name__ == "__main__":
    unittest.main()


__all__ = ["AlmaSessionTests"]
