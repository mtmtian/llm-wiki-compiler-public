"""Codex hooks call a stable launcher, so a runtime upgrade keeps the trusted hook unchanged.

Codex trusts each hook by a hash of its definition. A command that names the
immutable runtime directory changes on every deployment and needs re-trust.
The launcher resolves the installed runtime from the private config at run
time, like ``llmwiki-maintain``, so hooks.json and the launcher stay byte-identical
across upgrades while the executed hooks.py follows the installed runtime.
"""

import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import install
import test_install
from install_helpers import CODEX_HOOK_LAUNCHER, hook_command, own_hook, render_codex_hook


def legacy_command(runtime: Path, config: Path) -> str:
    """Return the runtime-pinned hook command written by installs before the launcher."""
    return shlex.join([sys.executable, str(runtime / "knowledge-flow/hooks.py"), "--config", str(config)])


class UpgradeKeepsTrustedHookTests(unittest.TestCase):
    """Run the real installer twice, as a deployment of a new runtime release does."""

    setUp = test_install.InstallerTests.setUp
    tearDown = test_install.InstallerTests.tearDown

    def test_upgrade_keeps_hook_definition_and_runs_new_runtime(self):
        """Given an install, When a new runtime is installed, Then hooks.json and the launcher
        are byte-identical and the launcher runs the new runtime's hooks.py."""
        install.install(self.args)
        hooks_path, launcher = self.home / ".codex/hooks.json", self.home / CODEX_HOOK_LAUNCHER
        before = hooks_path.read_bytes(), launcher.read_bytes()
        upgraded = self.root / "releases" / "next with spaces"
        shutil.copytree(self.runtime, upgraded)
        self.args.runtime = str(upgraded)
        install.install(self.args)
        self.assertEqual((hooks_path.read_bytes(), launcher.read_bytes()), before)
        (upgraded / "knowledge-flow/hooks.py").write_text("import sys\nprint('next', sys.argv[1:])\n", encoding="utf-8")
        config = self.home / ".config/llmwiki/knowledge-flow.json"
        output = subprocess.run([str(launcher), "--config", str(config)],
                                capture_output=True, text=True, check=True, timeout=10).stdout
        self.assertEqual(output.strip(), f"next ['--config', '{config}']")


class CodexHookLauncherTests(unittest.TestCase):
    """Exercise hook ownership and the launcher's argument contract."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "knowledge-flow.json"
        self.launcher = self.root / "bin" / "llmwiki-codex-hook"

    def test_legacy_pinned_entry_is_replaced_not_duplicated(self):
        """Given a hooks.json from a runtime-pinned install, Then the launcher entry replaces it."""
        legacy = legacy_command(self.root / "releases/old", self.config)
        original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": legacy}]}]}}
        command = hook_command(self.launcher, self.config)
        stop = install.build_hooks_for_config(original, command, self.config)["hooks"]["Stop"]
        commands = [entry["command"] for group in stop for entry in group["hooks"]]
        self.assertEqual(commands, [command])

    def test_both_forms_are_recognized_only_for_this_config(self):
        legacy = legacy_command(self.root / "releases/old", self.config)
        current = hook_command(self.launcher, self.config)
        other = hook_command(self.launcher, self.root / "other.json")
        for command, expected in ((legacy, True), (current, True), (other, False), ("keep-me", False)):
            self.assertEqual(own_hook({"type": "command", "command": command}, self.config), expected, command)

    def test_launcher_fails_closed_on_bad_arguments_or_missing_config(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text(render_codex_hook(sys.executable), encoding="utf-8")
        self.launcher.chmod(0o700)
        for argv in ([], ["--other", str(self.config)], ["--config", str(self.config)]):
            result = subprocess.run([str(self.launcher), *argv], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, argv)


if __name__ == "__main__":
    unittest.main()
