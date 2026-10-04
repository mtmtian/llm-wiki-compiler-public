"""Ambient host templates must not consume knowledge intake or review capacity.

These synthetic conversations exercise the normal hook boundary. Similar human
requests, additional user messages and incomplete templates stay eligible; a
model's JSON answer is never used to decide whether its user was a machine.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from common import load_json

SUGGESTION = """# Overview
Get an understanding of the user's intent and goals by deeply viewing their connected apps. Suggest actionable tasks that they would actually act on/click.
# Rules
Return 0 to 3 fresh suggestions.
ExampleProject catalog must use durable evidence.
# Examples
Suggest a catalog cleanup.
# Response format
- prompt: the user message to send
- pluginId: null.
- write the prompt as something that should launch as a new Codex task in this project
"""
SAFETY = """You are an expert at upholding safety and compliance standards for Codex ambient suggestions.
Your task is to determine if any suggestions should be excluded in order to adhere to the safety and compliance policies.
## 1. Policies to always exclude
Synthetic policy text.
## 2. Categories **about the user** to exclude **unless the user has specifically asked for it in recent context**
Synthetic category text.
# Ambient suggestion candidates
ExampleProject catalog candidate.
# Output Format
Return a JSON object with one field:
`exclude`
You must not output any other text. Only output the JSON object.
"""


class HostAutomationTests(unittest.TestCase):
    """Verify filtering before capacity checks without weakening normal capture."""

    def setUp(self):
        """Use an isolated, routed project with deferred workers."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"enabled": True, "stateDir": str(self.root), "eventDriven": {"enabled": True},
                       "projects": {"sample": {"aliases": ["ExampleProject"], "topicTerms": ["catalog"],
                                               "pages": []}}}
        self.event = {"session_id": "example-session", "turn_id": "example-turn", "cwd": str(self.root),
                      "hook_event_name": "UserPromptSubmit"}

    def submit(self, prompt, assistant="[]", supplied=None, omit_prompt=False):
        """Drive the real prompt/Stop events without invoking a worker."""
        event = {**self.event, "prompt": prompt}
        with patch.object(hooks, "invoke") as invoke:
            hooks.handle(event, self.config)
            stop = {**event, "hook_event_name": "Stop", "last_assistant_message": assistant}
            if omit_prompt:
                stop.pop("prompt")
            if supplied is not None:
                stop["conversation_evidence"] = supplied
            hooks.handle(stop, self.config)
            invoke.assert_not_called()
        return hooks.event_path(self.config, event).name

    def test_templates_complete_before_queue_capacity_or_artifact_bypass(self):
        """Given a full queue and a linked artifact, both host templates still finish empty."""
        self.config["maxQueuedJobs"] = 0
        artifact = self.root / "example.md"
        artifact.write_text("The catalog must retain its source attribution.", encoding="utf-8")
        for index, prompt in enumerate((SUGGESTION, SAFETY)):
            with self.subTest(template=index):
                self.event["turn_id"] = f"template-{index}"
                name = self.submit(prompt, f"[catalog]({artifact})")
                result = load_json(self.root / "completed" / name, {})
                self.assertEqual(result.get("status"), "empty")
                self.assertTrue(result.get("reason", "").startswith("host-ambient-"))
                self.assertFalse((self.root / "capture-errors" / name).exists())
        self.assertFalse((self.root / "queue").exists())

    def test_long_template_is_classified_before_prompt_truncation(self):
        """Given a complete long prompt, a Stop omitting it retains the exact intake decision."""
        prompt = SAFETY.replace("Synthetic policy text.", "ExampleProject catalog policy.\n" * 700)
        name = self.submit(prompt, omit_prompt=True)
        self.assertEqual(load_json(self.root / "completed" / name)["reason"], "host-ambient-safety")

    def test_similar_human_requests_and_json_answers_are_retained(self):
        """Given a human request or an incomplete/quoted template, JSON does not suppress it."""
        prompts = ("ExampleProject catalog: suggest improvements and return JSON.",
                   "Explain this ExampleProject catalog template:\n" + SUGGESTION,
                   SUGGESTION + "\nAlso explain the catalog tradeoffs.")
        for index, prompt in enumerate(prompts):
            with self.subTest(prompt=index):
                self.event["turn_id"] = f"human-{index}"
                name = self.submit(prompt)
                self.assertTrue((self.root / "queue" / name).exists())

    def test_truncated_historical_template_stays_eligible(self):
        """Given only a template prefix, the missing suffix cannot be inferred."""
        prompt = SAFETY.replace("Synthetic policy text.", "ExampleProject catalog policy.\n" * 700)[:12000]
        name = self.submit(prompt)
        self.assertTrue((self.root / "queue" / name).exists())

    def test_explicit_requeue_also_filters_complete_automation_evidence(self):
        """Given a retained old source, intake applies the same template boundary."""
        queued = self.root / "queue" / "old-source.json"
        hooks.enqueue_job({"sessionId": "example-session", "prompt": SUGGESTION,
                           "evidence": [{"kind": "user", "text": SUGGESTION, "locator": "codex://example-session/turn/old"}]}, queued, self.config)
        self.assertFalse(queued.exists())
        self.assertEqual(load_json(self.root / "completed" / queued.name)["reason"], "host-ambient-suggestions")

    def test_additional_current_user_overrides_a_template_marker(self):
        """Given a complete host template and a human follow-up, the whole turn remains eligible."""
        name = self.submit(SUGGESTION, supplied=[{"kind": "user", "text": SUGGESTION},
                                                {"kind": "user", "text": "Explain the catalog tradeoff."}])
        self.assertTrue((self.root / "queue" / name).exists())

    def test_other_host_does_not_use_codex_template_filter(self):
        """Given the same words from another adapter, no Codex-only provenance is inferred."""
        for host in ("claude", "pi"):
            with self.subTest(host=host):
                self.event["session_id"] = f"{host}:example-profile:example-session"
                name = self.submit(SUGGESTION)
                self.assertTrue((self.root / "queue" / name).exists())

    def test_alma_native_locator_preserves_identical_human_text(self):
        """Alma uses an unprefixed session ID, so its native locator still separates the host."""
        name = self.submit(SUGGESTION, supplied=[{"kind": "user", "text": SUGGESTION,
                                                "locator": "alma://thread/example-message"}])
        self.assertTrue((self.root / "queue" / name).exists())

    def test_full_host_evidence_cannot_be_hidden_by_short_prompt_record(self):
        """Given verified full evidence, the template is filtered even without a prompt marker."""
        name = self.submit("ExampleProject catalog", supplied=[{"kind": "user", "text": SAFETY}])
        self.assertEqual(load_json(self.root / "completed" / name)["reason"], "host-ambient-safety")


if __name__ == "__main__":
    unittest.main()
