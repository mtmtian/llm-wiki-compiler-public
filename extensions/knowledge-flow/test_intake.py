"""Real prompt-to-Stop routing and deferred intake, with no model or shared writes."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from common import load_json


class AutomaticIntakeTests(unittest.TestCase):
    """Normal business work is captured without a special request to save it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root),
                       "eventDriven": {"enabled": True, "debounceSeconds": 120},
                       "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放"],
                                                "requiredTerms": ["PC"], "pages": []}}}
        self.event = {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t",
                      "cwd": str(self.root), "prompt": "ProductX PC 投放的两组方案分别有什么适用条件？"}

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, assistant):
        """Drive the same native event sequence used by the installed hooks."""
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": assistant}
        hooks.handle(stop, self.config)
        return stop

    def test_normal_business_turn_is_queued_without_inline_model(self):
        with patch.object(hooks, "invoke", return_value={"context": "", "seen": {}, "status": "no-hit",
                                                          "references": [], "complete": True}) as invoke:
            stop = self.submit("同龄 cohort 能减少观察窗口差异造成的偏差；下一轮比较需先对齐观察天数。")
            self.assertTrue(all(call.args[1] == "context" for call in invoke.call_args_list))
            hooks.handle(stop, self.config)
        files = list((self.root / "queue").glob("*.json"))
        self.assertEqual(len(files), 1)
        job = load_json(files[0])
        self.assertEqual(job["projectId"], "growth")
        self.assertEqual([item["kind"] for item in job["evidence"]], ["user", "assistant"])
        self.assertIn("notBefore", job)

    def test_unrelated_turn_and_ambiguous_platform_do_not_enter_queue(self):
        for prompt in ("今天天气如何", "ProductX 投放怎么比较"):
            self.event["prompt"] = prompt
            with patch.object(hooks, "invoke", return_value={"context": "", "seen": {}, "status": "no-hit",
                                                              "references": [], "complete": True}) as invoke:
                self.submit("这是一段有长度但不应绕过项目隔离的助手输出。")
                self.assertTrue(all(call.args[1] == "context" for call in invoke.call_args_list))
        self.assertFalse((self.root / "queue").exists())

    def test_acknowledgement_instructions_do_not_queue_model_work(self):
        """Given a routed turn, When only an acknowledgement is requested, Then no work queues."""
        for index, prompt in enumerate(("只回复：好", "请只回答 好", "只说 ok", "回复收到。")):
            with self.subTest(prompt=prompt):
                queued = self.root / "queue" / f"ack-{index}.json"
                job = {"prompt": prompt, "evidence": [
                    {"kind": "user", "text": prompt}, {"kind": "assistant", "text": "好"}]}
                hooks.enqueue_job(job, queued, self.config)
                self.assertFalse(queued.exists())
                self.assertEqual(load_json(self.root / "completed" / queued.name)["status"], "empty")

    def test_acknowledgement_filter_preserves_substantive_decisions(self):
        """Given a real decision, When wording requests confirmation, Then its evidence still queues."""
        prompt = "请确认采用方案 A，以后都按成熟窗口评估投放。"
        queued = self.root / "queue/decision.json"
        job = {"prompt": prompt, "evidence": [
            {"kind": "user", "text": prompt}, {"kind": "assistant", "text": "好"}]}
        hooks.enqueue_job(job, queued, self.config)
        self.assertEqual(load_json(queued)["evidence"][0]["text"], prompt)


if __name__ == "__main__":
    unittest.main()
