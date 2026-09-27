"""Isolated user-visible checks for host adapter install, status and disable."""

from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from deployment.agent_plugins.install_settings import current_command
from deployment.agent_plugins.installer import INSTALL_ROOT, prepare_file_plan
from deployment.install_agents import parser
from deployment.agent_plugins.installer import install, status


class InstallHome:
    """Disposable home with one shared config and the existing MCP launcher."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve()
        self.config = self.path / ".config/llmwiki/knowledge-flow.json"
        self.config.parent.mkdir(parents=True)
        self.save_config({"version": 1, "enabled": True, "customPolicy": {"keep": 7}})
        launcher = self.path / ".local/bin/llmwiki-local"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o700)

    def close(self) -> None:
        self.temp.cleanup()

    def __enter__(self) -> InstallHome:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_config(self, value: dict) -> None:
        self.config.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def args(self, action: str, host: str, **values: object) -> Namespace:
        return Namespace(action=action, host=host, home=self.path, config=None,
                         profile=values.get("profile"), pi_settings=values.get("pi_settings"),
                         dry_run=bool(values.get("dry_run", False)))


class AgentInstallerTests(unittest.TestCase):
    """Exercise reversible native settings changes without using a live home."""

    def setUp(self) -> None:
        self.home = InstallHome()

    def tearDown(self) -> None:
        self.home.close()

    def run_install(self, host: str, **values: object) -> dict:
        return install(self.home.args("install", host, **values))

    def run_disable(self, host: str, **values: object) -> dict:
        return install(self.home.args("disable", host, **values))

    def test_dry_run_and_shared_registration_are_idempotent(self) -> None:
        root = self.home.path / INSTALL_ROOT
        dry = install(self.home.args("install", "claude", dry_run=True))
        self.assertTrue(dry["dryRun"])
        self.assertFalse(root.exists())
        self.run_install("claude")
        self.run_install("pi")
        self.assertEqual([], self.run_install("claude")["changes"])
        self.assertEqual([], self.run_install("pi")["changes"])
        records = self.home.read_config()["agentPlugins"]["registrations"]
        self.assertEqual({"claude", "pi"}, {item["host"] for item in records})
        self.assertEqual({str(self.home.config)}, {item["config"] for item in records})

    def test_status_distinguishes_registration_from_runtime_delivery(self) -> None:
        self.run_install("claude")
        result = status(self.home.args("status", "claude"))
        row = result["registrations"][0]
        self.assertTrue(row["enabled"])
        self.assertTrue(row["mcpConfigured"])
        self.assertEqual("unverified", row["runtimeDelivery"])

    def test_disabling_one_host_leaves_other_host_and_user_servers(self) -> None:
        other = {"type": "stdio", "command": "/user/server", "args": []}
        claude_mcp = self.home.path / ".claude.json"
        pi_mcp = self.home.path / ".pi/agent/mcp.json"
        write_json(claude_mcp, {"mcpServers": {"other": other}})
        write_json(pi_mcp, {"mcpServers": {"other": other}})
        self.run_install("claude")
        self.run_install("pi")
        self.run_disable("claude")
        records = self.home.read_config()["agentPlugins"]["registrations"]
        self.assertEqual(["pi"], [item["host"] for item in records])
        self.assertTrue((self.home.path / ".pi/agent/settings.json").exists())
        self.assertEqual({"other": other, "llmwiki": {
            "type": "stdio", "command": str(self.home.path / ".local/bin/llmwiki-local"), "args": ["serve"]
        }}, json.loads(pi_mcp.read_text())["mcpServers"])
        self.assertEqual({"other": other}, json.loads(claude_mcp.read_text())["mcpServers"])
        self.run_disable("pi")
        self.assertNotIn("agentPlugins", self.home.read_config())
        self.assertEqual({"other": other}, json.loads(pi_mcp.read_text())["mcpServers"])

    def test_profiles_disable_independently_and_unknown_hooks_survive(self) -> None:
        default = self.home.path / ".claude"
        claudex = self.home.path / ".claude/profiles/claudex"
        first = default / "settings.json"
        other = claudex / "settings.json"
        unrelated = {"type": "command", "command": "user-command"}
        write_json(first, {"hooks": {"UserPromptSubmit": [{"hooks": [unrelated]}], "Other": []}})
        self.run_install("claude", profile=[str(default)])
        self.run_install("claude", profile=[str(claudex)])
        self.run_install("pi")
        self.run_disable("claude", profile=[str(default)])
        records = self.home.read_config()["agentPlugins"]["registrations"]
        self.assertEqual({("claude", str(claudex.resolve())), ("pi", str((self.home.path / ".pi/agent").resolve()))},
                         {(item["host"], item["profile"]) for item in records})
        settings = json.loads(first.read_text())
        self.assertEqual([unrelated], settings["hooks"]["UserPromptSubmit"][0]["hooks"])
        self.assertTrue(json.loads(other.read_text())["hooks"]["Stop"])

    def test_unowned_mcp_collision_fails_before_any_write(self) -> None:
        target = self.home.path / ".claude.json"
        write_json(target, {"mcpServers": {"llmwiki": {"command": "user-owned"}}})
        before_config, before_mcp = self.home.config.read_bytes(), target.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists outside"):
            self.run_install("claude")
        self.assertEqual(before_config, self.home.config.read_bytes())
        self.assertEqual(before_mcp, target.read_bytes())
        self.assertFalse((self.home.path / INSTALL_ROOT).exists())

    def test_planning_time_settings_change_aborts_without_overwrite(self) -> None:
        args = self.home.args("install", "claude")
        plan = prepare_file_plan(args)
        target = self.home.path / ".claude/settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"hooks":{"Other":[]}}\n', encoding="utf-8")
        changed = target.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed after planning"):
            install(args, plan)
        self.assertEqual(changed, target.read_bytes())
        self.assertFalse((self.home.path / INSTALL_ROOT).exists())

    def test_modified_immutable_bundle_is_preserved_and_rejected(self) -> None:
        self.run_install("claude")
        current = self.home.path / INSTALL_ROOT / "current"
        digest = current.readlink()
        bundle = current.resolve()
        target = bundle / "bridge.py"
        target.chmod(0o600)
        target.write_text("changed\n", encoding="utf-8")
        damaged = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "agent bundle file was changed"):
            self.run_install("claude")
        self.assertEqual(digest, current.readlink())
        self.assertEqual(damaged, target.read_bytes())

    def test_pi_rejects_a_non_default_policy_config(self) -> None:
        custom = self.home.path / "private.json"
        custom.write_text('{"version":1}\n', encoding="utf-8")
        args = self.home.args("install", "pi")
        args.config = custom
        with self.assertRaisesRegex(ValueError, "Pi reads the shared default config"):
            prepare_file_plan(args)

    def test_legacy_hooks_are_removed_only_for_same_config_and_profile(self) -> None:
        profile = self.home.path / ".claude"
        settings = profile / "settings.json"
        script = self.home.path / ".local/share/llm-wiki-compiler/integrations/claude/abcdef012345/claude_hook.py"
        same = legacy_entry(script, self.home.config, profile)
        foreign = legacy_entry(script, self.home.path / "other-config.json", profile)
        unrelated = {"type": "command", "command": "keep-this-hook"}
        current = {"type": "command", "command": current_command(
            self.home.path / ".local/bin/llmwiki-agent", self.home.config, profile), "timeout": 15}
        write_json(settings, {"hooks": {"Stop": [
            {"hooks": [current]}, {"hooks": [same]}, {"hooks": [foreign, unrelated]}]}})
        self.run_install("claude")
        groups = json.loads(settings.read_text())["hooks"]["Stop"]
        entries = [entry for group in groups for entry in group["hooks"]]
        commands = [entry["command"] for entry in entries]
        self.assertEqual(1, sum(current["command"] == command for command in commands))
        self.assertEqual(1, sum(str(script) in command for command in commands))
        self.assertTrue(any(str(self.home.path / "other-config.json") in command for command in commands))
        self.assertIn("keep-this-hook", commands)
        self.run_disable("claude")
        entries = json.loads(settings.read_text())["hooks"]["Stop"][0]["hooks"]
        self.assertEqual([foreign, unrelated], entries)

    def test_cli_documents_dryrun_status_and_reversible_disable(self) -> None:
        parsed = parser().parse_args(["install", "--host", "pi", "--dry-run", "--home", str(self.home.path)])
        self.assertEqual("install", parsed.action)
        self.assertTrue(parsed.dry_run)
        self.assertEqual("pi", parsed.host)


def write_json(path: Path, value: dict) -> None:
    """Write a fixture JSON file and create only its isolated parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def legacy_entry(script: Path, config: Path, profile: Path) -> dict:
    """Return the exact old bridge shape used for ownership regression tests."""
    command = f"python3 -B {script} --config {config} --claude-config-dir {profile}"
    return {"type": "command", "command": command, "timeout": 15}


if __name__ == "__main__":
    unittest.main()
