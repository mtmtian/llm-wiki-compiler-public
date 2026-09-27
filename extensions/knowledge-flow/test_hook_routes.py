"""Given projectless tasks, verify routing identity and evidence survive host events."""
import tempfile
import unittest
from pathlib import Path

from common import load_json
from hooks import event_path, handle


class ProjectlessHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root / "state"),
                       "owners": ["mine"], "workingForks": ["mine/service"], "projects": {},
                       "exchange": {"root": str(self.root / "exchange")}}
        self.event = {"cwd": str(self.root), "session_id": "s", "turn_id": "one",
                      "hook_event_name": "UserPromptSubmit",
                      "prompt": "https://github.com/mine/service 以后保留稳定ID"}

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_repo_identity_survives_projectless_intake(self):
        """Given an eligible URL outside a repo, When Stop runs, Then its evidence and identity enter one job."""
        handle(self.event, self.config)
        turn = load_json(event_path(self.config, self.event))
        self.assertEqual(turn["projectId"], "repo-mine-service")
        self.event["hook_event_name"] = "Stop"
        handle(self.event, self.config)
        jobs = list((Path(self.config["stateDir"]) / "queue").glob("*.json"))
        self.assertEqual(len(jobs), 1)
        job = load_json(jobs[0])
        self.assertEqual(job["repoIdentity"], "mine/service")
        self.assertEqual(job["evidence"][0]["text"], self.event["prompt"])

    def test_short_followup_keeps_repo_but_bare_assent_is_not_evidence(self):
        """Given a bound repo, When a user only says yes, Then identity persists and no factual job is created."""
        handle(self.event, self.config)
        self.event.update(turn_id="two", prompt="可以")
        handle(self.event, self.config)
        turn = load_json(event_path(self.config, self.event))
        self.assertEqual(turn["projectId"], "repo-mine-service")
        self.assertEqual(turn["repoIdentity"], "mine/service")
        self.event["hook_event_name"] = "Stop"
        handle(self.event, self.config)
        completed = Path(self.config["stateDir"]) / "completed" / event_path(self.config, self.event).name
        self.assertEqual(load_json(completed)["reason"], "no-durable-evidence")
        self.assertFalse((Path(self.config["stateDir"]) / "queue").exists())

    def test_substantive_permission_constraint_is_queued(self):
        """Given an identified project and concrete constraint, When Stop runs, Then it reaches independent review."""
        self.event["prompt"] = "https://github.com/mine/service 可以一次多要些权限，避免频繁申请"
        handle(self.event, self.config)
        self.event["hook_event_name"] = "Stop"
        handle(self.event, self.config)
        jobs = list((Path(self.config["stateDir"]) / "queue").glob("*.json"))
        self.assertEqual(len(jobs), 1)
        self.assertIn("避免频繁申请", load_json(jobs[0])["evidence"][0]["text"])


if __name__ == "__main__":
    unittest.main()
