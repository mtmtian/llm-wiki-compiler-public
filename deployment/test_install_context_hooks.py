"""Behavioral tests for runtime helper shipping and lifecycle hook wiring."""

from __future__ import annotations

import unittest
from pathlib import Path

import install


class InstallContextHookTests(unittest.TestCase):
    """Keep lifecycle reads bounded while preserving unrelated user hooks."""

    def test_runtime_allowlist_contains_all_read_context_helpers(self):
        required = {
            "knowledge-flow/read_routing.py",
            "knowledge-flow/hook_context.py",
            "knowledge-flow/context_observation.py",
            "knowledge-flow/operational_context.py",
        }
        self.assertTrue(required.issubset(set(install.RUNTIME_FILES)))

    def test_session_lifecycle_hooks_and_prompt_budget_are_idempotent(self):
        config_path = Path("/tmp/llmwiki-context-test.json")
        command = "/usr/bin/python3 /tmp/runtime/knowledge-flow/hooks.py --config /tmp/llmwiki-context-test.json"
        original = {
            "hooks": {
                "UserPromptSubmit": [{"matcher": "keep", "hooks": [{"type": "command", "command": "keep-prompt"}]}],
                "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "keep-session"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}],
                "Notification": [{"hooks": [{"type": "command", "command": "keep-notification"}]}],
            }
        }

        rendered = install.build_hooks_for_config(original, command, config_path)
        hooks = rendered["hooks"]
        prompt = hooks["UserPromptSubmit"][-1]["hooks"][0]
        session = hooks["SessionStart"][-1]
        stop = hooks["Stop"][-1]["hooks"][0]

        self.assertEqual(prompt["additionalContextLimit"], 0)
        self.assertEqual(session["matcher"], "resume|clear|compact")
        self.assertEqual(session["hooks"][0]["additionalContextLimit"], 0)
        self.assertNotIn("additionalContextLimit", stop)
        self.assertEqual(stop["timeout"], 600)
        self.assertIn("keep-prompt", str(hooks["UserPromptSubmit"]))
        self.assertIn("keep-session", str(hooks["SessionStart"]))
        self.assertIn("keep-stop", str(hooks["Stop"]))
        self.assertIn("keep-notification", str(hooks["Notification"]))
        self.assertEqual(rendered, install.build_hooks_for_config(rendered, command, config_path))


if __name__ == "__main__":
    unittest.main()
