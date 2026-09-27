"""Local notification delivery is deduplicated and never needs a model."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from common import save_json
from notify import notify_actionable


class NotificationTests(unittest.TestCase):
    def test_quiet_state_and_repeated_issue_do_not_notify_again(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"stateDir": directory}
            runner = Mock(return_value=SimpleNamespace(returncode=0))
            notify_actionable(config, {}, runner, "Darwin")
            runner.assert_not_called()
            save_json(Path(directory) / "review" / "job.json", {"private": "business content"})
            self.assertTrue(notify_actionable(config, {}, runner, "Darwin")["sent"])
            notify_actionable(config, {}, runner, "Darwin")
            self.assertEqual(runner.call_count, 1)
            self.assertNotIn("business content", str(runner.call_args))

    def test_failed_delivery_is_not_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"stateDir": directory}
            runner = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
            self.assertFalse(notify_actionable(config, {"reasons": ["processing-error"]}, runner, "Darwin")["sent"])
            self.assertTrue(notify_actionable(config, {"reasons": ["processing-error"]}, runner, "Darwin")["sent"])

    def test_capture_failure_notifies_without_exposing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            save_json(Path(directory) / "capture-errors" / "job.json", {"private": "business content"})
            runner = Mock(return_value=SimpleNamespace(returncode=0))
            self.assertTrue(notify_actionable({"stateDir": directory}, {}, runner, "Darwin")["sent"])
            self.assertIn("采集异常 1", str(runner.call_args))
            self.assertNotIn("business content", str(runner.call_args))


if __name__ == "__main__":
    unittest.main()
