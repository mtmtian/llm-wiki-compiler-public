"""The maintenance CLI refuses concurrent dismissals instead of waiting behind the worker."""

import contextlib
import fcntl
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance


class MaintenanceResolveTests(unittest.TestCase):
    """Explicit hold resolution shares the retry worker lock without long waits."""

    def test_dismiss_returns_busy_when_worker_lock_is_held(self):
        """Given an active worker, When CLI dismiss runs, Then it returns busy without invoking Node."""
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            lock_path = state / "worker.lock"
            config = {"stateDir": str(state)}
            output = io.StringIO()
            with lock_path.open("a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(sys, "argv", ["maintenance.py", "--config", "unused", "--resolve",
                                                  "batch-full", "--action", "dismiss"]), \
                        patch("maintenance.config_from", return_value=config), \
                        patch("maintenance.invoke") as invoke, contextlib.redirect_stdout(output):
                    maintenance.main()

        self.assertEqual(json.loads(output.getvalue()), {"resolved": False, "busy": True})
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
