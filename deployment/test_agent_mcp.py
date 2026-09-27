"""Native MCP configuration tests using temporary host files, never credentials."""

import json
import tempfile
import unittest
from pathlib import Path

from agent_mcp import mcp_path, prepare_mcp_plan, server_spec, update_servers


class McpRegistrationTests(unittest.TestCase):
    """Check ownership boundaries and side-effect-free native registration plans."""

    def test_preserves_other_servers_and_host_settings(self):
        original = {"mcpServers": {"codegraph": {"command": "codegraph"}}, "theme": "dark"}
        expected = server_spec(Path("/launcher"))
        added, owned = update_servers(original, expected, True, False)
        self.assertTrue(owned)
        self.assertNotIn("llmwiki", original["mcpServers"])
        self.assertEqual(added["mcpServers"]["codegraph"], original["mcpServers"]["codegraph"])
        removed, owned = update_servers(added, expected, False, True)
        self.assertEqual(removed, original)
        self.assertFalse(owned)

    def test_never_adopts_unowned_or_replaces_changed_server(self):
        expected = server_spec(Path("/launcher"))
        for previous, actual in [(False, expected), (True, {"command": "user-replacement"})]:
            with self.subTest(previous=previous):
                original = {"mcpServers": {"llmwiki": actual}}
                with self.assertRaises(ValueError):
                    update_servers(original, expected, True, previous)
                if actual != expected:
                    self.assertEqual(update_servers(original, expected, False, previous)[0], original)

    def test_native_profile_paths(self):
        home = Path("/fixture")
        self.assertEqual(mcp_path("claude", home / ".claude", home), home / ".claude.json")
        self.assertEqual(mcp_path("claude", home / "profile", home), home / "profile/.claude.json")
        self.assertEqual(mcp_path("pi", home / ".pi/agent", home), home / ".pi/agent/mcp.json")

    def test_plan_is_read_only_and_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            launcher = home / "launcher"
            launcher.write_text("#!/bin/sh\n")
            profile = home / "pi"
            item, owned = prepare_mcp_plan("pi", profile, home, launcher, True, False)
            self.assertTrue(owned)
            self.assertFalse(profile.exists())
            target, content, _ = item
            target.parent.mkdir()
            target.write_text(content)
            self.assertEqual(json.loads(content)["mcpServers"]["llmwiki"], server_spec(launcher))
            self.assertEqual(prepare_mcp_plan("pi", profile, home, launcher, True, True), (None, True))


if __name__ == "__main__":
    unittest.main()
