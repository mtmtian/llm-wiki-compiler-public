"""Tests for portable host installation without touching the real HOME."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import install

class InstallerTests(unittest.TestCase):
    """Exercise role selection, preservation, idempotence, and preflight safety."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.wiki = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/wiki with spaces"
        self.wiki.mkdir(parents=True)
        self.runtime = self.root / "runtime with spaces"
        for relative in install.RUNTIME_FILES:
            target = self.runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# intakeEnabled\n", encoding="utf-8")
        manifest = {"commit": "0123456789abcdef0123456789abcdef01234567", "files": {
            relative: hashlib.sha256((self.runtime / relative).read_bytes()).hexdigest()
            for relative in install.RUNTIME_FILES}}
        (self.runtime / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        legacy = json.loads((Path(__file__).parent / "knowledge-flow.json").read_text())
        legacy.pop("exchange", None)
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.node_log = self.root / "node-args.log"
        for name in ("node", "gh"):
            target = self.bin / name
            target.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"${FAKE_NODE_LOG:-/dev/null}\"\nprintf 'v24.0.0\\n'\n", encoding="utf-8")
            target.chmod(0o700)
        self.args = Namespace(runtime=str(self.runtime), wiki_root=str(self.wiki), config_source=None,
                               writer=False, contributor=False, publisher=False, reader=False,
                               machine_id=None, dry_run=False)
        self.args.config_source = str(legacy_path)
        self.env = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        self.binaries = patch.object(install, "find_binary", side_effect=lambda name: str(self.bin / name))
        self.binaries.start()
        self.node_check = patch.object(install, "check_node")
        self.node_check.start()

    def tearDown(self):
        self.node_check.stop()
        self.binaries.stop()
        self.env.stop()
        self.temp.cleanup()

    def config_path(self) -> Path:
        """Return the isolated private config path."""
        return self.home / ".config/llmwiki/knowledge-flow.json"

    def exchange_source(self) -> Path:
        """Create the shared two-machine template used by role tests."""
        source = json.loads((Path(__file__).parent / "knowledge-flow.json").read_text())
        source["exchange"] = {"protocolVersion": 1,
                              "root": "${WIKI_ROOT}/.knowledge-exchange",
                              "publisherMachineId": "peer-a",
                              "participants": ["peer-a", "peer-b"]}
        path = self.root / "exchange.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        return path

    def v2_exchange_source(self) -> Path:
        """Create the multi-publisher exchange template used by v2 tests."""
        source = json.loads((Path(__file__).parent / "knowledge-flow.json").read_text())
        source["exchange"] = {"protocolVersion": 2,
                              "root": "${WIKI_ROOT}/.knowledge-exchange",
                              "legacyImporterMachineId": "peer-a",
                              "participants": ["peer-a", "peer-b"]}
        path = self.root / "exchange-v2.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        return path

    def test_public_template_omits_machine_specific_exchange_policy(self):
        """The public default carries no private participants or project routing."""
        template = json.loads((Path(__file__).parent / "knowledge-flow.json").read_text())
        for key in ("owners", "workingForks", "checks", "exchange"):
            self.assertNotIn(key, template)
        self.assertEqual(template["projects"], {})

    def test_coordinated_default_install_preserves_both_publication_and_reinstall(self):
        """Given an explicit default change, install stages coordination without writing Wiki."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id, self.args.writer = 'peer-a', True
        self.args.materializer_machine_id = 'peer-a'
        self.args.shared_writer_bootstrap_machine = 'peer-b'
        self.args.dry_run = True
        install.install(self.args)
        self.assertFalse(self.config_path().exists())
        self.assertEqual(list(self.wiki.iterdir()), [])
        self.args.dry_run = False
        install.install(self.args)
        before = json.loads(self.config_path().read_text())
        self.assertTrue(before['intakeEnabled'] and before['publishEnabled'])
        self.assertEqual(before['exchange']['materializerMachineId'], 'peer-a')
        self.assertEqual(before['exchange']['sharedWriter']['bootstrapMachineId'], 'peer-b')
        self.args.materializer_machine_id = self.args.shared_writer_bootstrap_machine = None
        install.install(self.args)
        self.assertEqual(json.loads(self.config_path().read_text()), before)
        self.assertEqual(list(self.wiki.iterdir()), [])

    def test_default_change_without_old_owner_fails_before_installation(self):
        """Given incomplete coordination, installation cannot silently create a second writer."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id, self.args.writer = 'peer-a', True
        self.args.materializer_machine_id = 'peer-a'
        with self.assertRaisesRegex(ValueError, 'both materializer and bootstrap'):
            install.install(self.args)
        self.assertFalse(self.config_path().exists())

    def test_v2_contributor_requires_a_valid_declared_legacy_importer(self):
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-b"
        self.args.contributor = True
        self.args.dry_run = True
        install.install(self.args)
        source_path = Path(self.args.config_source)
        source = json.loads(source_path.read_text())
        source["exchange"].pop("legacyImporterMachineId")
        source_path.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "legacyImporterMachineId"):
            install.install(self.args)
        for invalid in (None, "unknown", []):
            with self.subTest(importer=invalid):
                source["exchange"]["legacyImporterMachineId"] = invalid
                source_path.write_text(json.dumps(source))
                with self.assertRaises(ValueError):
                    install.install(self.args)

    def test_v2_writer_or_publisher_can_publish_on_each_participant(self):
        """Given two participants, writer and publisher both enable local publication."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-a"
        self.args.writer = True
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertTrue(config["intakeEnabled"])
        self.assertTrue(config["publishEnabled"])
        self.args.machine_id = "peer-b"
        self.args.writer = False
        self.args.publisher = True
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertTrue(config["intakeEnabled"])
        self.assertTrue(config["publishEnabled"])

    def test_v2_unknown_machine_is_rejected(self):
        """Given a v2 exchange, an undeclared machine cannot install a writer."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "unknown-machine"
        self.args.writer = True
        with self.assertRaisesRegex(ValueError, "participants"):
            install.install(self.args)

    def test_v2_upgrade_keeps_shared_root_and_moves_state_root_local(self):
        """Given an existing v2 config, reinstalling uses sharedWikiRoot over stale wikiRoot."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-a"
        self.args.wiki_root = str(self.wiki)
        self.args.writer = True
        install.install(self.args)
        first = json.loads(self.config_path().read_text())
        local_root = Path(first["wikiRoot"])
        self.assertEqual(Path(first["sharedWikiRoot"]), self.wiki.resolve())
        self.assertFalse(local_root.exists())
        self.args.wiki_root = None
        self.args.writer = False
        self.args.publisher = False
        install.install(self.args)
        second = json.loads(self.config_path().read_text())
        self.assertEqual(Path(second["sharedWikiRoot"]), self.wiki.resolve())
        self.assertEqual(Path(second["wikiRoot"]), local_root)

    def test_v2_dry_run_allows_uninitialized_replica_root(self):
        """Given an existing shared root and no replica view, dry-run still renders a plan."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-a"
        self.args.writer = True
        self.args.dry_run = True
        result = install.install(self.args)
        self.assertTrue(result["dryRun"])
        self.assertFalse(self.config_path().exists())

    def test_v2_event_dry_run_reports_shared_directory_creation(self):
        """Given a prepared writer, preview reports directories; only install creates them."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-a"
        self.args.writer = True
        self.args.event_driven = True
        self.args.dry_run = True
        codex = self.bin / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o700)
        publications = self.wiki / ".knowledge-exchange/v2/publications"
        with patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath}):
            result = install.install(self.args)
            directories = [item for item in result["changes"] if item.get("kind") == "directory"]
            self.assertIn({"path": "$WIKI_ROOT/.knowledge-exchange/v2/publications/peer-a",
                           "action": "create", "kind": "directory"}, directories)
            self.assertFalse(publications.exists() or self.config_path().exists())
            self.args.dry_run = False
            installed = install.install(self.args)
            self.assertEqual(installed["changes"], result["changes"])
            self.assertTrue(all((publications / item).is_dir() for item in ("peer-a", "peer-b")))
            repeated = install.install(self.args)
        self.assertTrue(all(item["action"] == "unchanged" for item in repeated["changes"]))
        self.assertFalse((publications.parent / "baseline.json").exists())
        self.assertEqual(list(publications.rglob("*.json")) + list(self.wiki.rglob("*.md")), [])

    def test_local_launcher_fails_closed_when_v2_replica_is_missing(self):
        """Given a v2 install without a local replica view, launch never falls back to shared Wiki."""
        self.args.config_source = str(self.v2_exchange_source())
        self.args.machine_id = "peer-a"
        self.args.writer = True
        install.install(self.args)
        launcher = self.home / ".local/bin/llmwiki-local"
        env = os.environ.copy()
        env["FAKE_NODE_LOG"] = str(self.node_log)
        result = subprocess.run([str(launcher), "status"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not initialized", result.stderr)
        self.assertFalse(self.node_log.exists())

    def test_default_reader_and_explicit_writer(self):
        """New machines read by default; writer is an explicit switch."""
        hooks_path = self.home / ".codex/hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}), encoding="utf-8")
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertFalse(config["intakeEnabled"])
        hooks = json.loads(hooks_path.read_text())["hooks"]
        self.assertEqual(sum(len(group["hooks"]) for group in hooks["Stop"]), 2)
        self.assertIn("keep", json.dumps(hooks))
        self.args.writer = True
        install.install(self.args)
        self.assertTrue(json.loads(self.config_path().read_text())["intakeEnabled"])

    def test_idempotent_and_dry_run(self):
        """A second run is unchanged and dry-run does not create private files."""
        self.args.dry_run = True
        result = install.install(self.args)
        self.assertTrue(result["dryRun"])
        self.assertFalse(self.config_path().exists())
        self.args.dry_run = False
        install.install(self.args)
        result = install.install(self.args)
        self.assertTrue(all(item["action"] == "unchanged" for item in result["changes"]))
        backups = self.home / ".local/share/llm-wiki-compiler/install-backups"
        self.assertEqual(len(list(backups.iterdir())), 1)
        os.chmod(self.config_path(), 0o644)
        result = install.install(self.args)
        self.assertEqual(result["changes"][0]["action"], "update")
        self.assertEqual(self.config_path().stat().st_mode & 0o777, 0o600)

    def test_stop_hook_uses_supported_synchronous_dispatch(self):
        """Codex skips async command hooks; shared Stop queues synchronously."""
        install.install(self.args)
        hooks = json.loads((self.home / ".codex/hooks.json").read_text())["hooks"]
        stop = hooks["Stop"][-1]["hooks"][0]
        self.assertNotIn("async", stop)
        self.assertEqual(stop["type"], "command")

    def test_machine_id_is_preserved_when_supplied(self):
        """An explicit stable machine identity is recorded without being invented."""
        machine_path = self.home / ".config/llmwiki/machine.json"
        machine_path.parent.mkdir(parents=True)
        machine_path.write_text(json.dumps({"machineId": "example-machine-01"}), encoding="utf-8")
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertEqual(config["machineId"], "example-machine-01")

    def test_invalid_machine_id_is_rejected_before_write(self):
        """Malformed machine identities fail preflight and create no config."""
        machine_path = self.home / ".config/llmwiki/machine.json"
        machine_path.parent.mkdir(parents=True)
        machine_path.write_text(json.dumps({"machineId": "old machine/name"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "machineId"):
            install.install(self.args)
        self.assertFalse(self.config_path().exists())

    def test_machine_wiki_override_and_local_launcher(self):
        """Machine path wins over the default and the launcher sources shell env."""
        machine_wiki = self.root / "machine wiki"
        machine_wiki.mkdir()
        machine_path = self.home / ".config/llmwiki/machine.json"
        machine_path.parent.mkdir(parents=True)
        machine_path.write_text(json.dumps({"wikiRoot": str(machine_wiki)}), encoding="utf-8")
        self.args.wiki_root = None
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertEqual(config["wikiRoot"], str(machine_wiki.resolve()))
        launcher = self.home / ".local/bin/llmwiki-local"
        env = os.environ.copy()
        env["FAKE_NODE_LOG"] = str(self.node_log)
        result = subprocess.run([str(launcher), "status"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("v24.0.0", result.stdout)
        self.assertEqual(self.node_log.read_text().splitlines()[0], str((self.runtime / "dist/cli.js").resolve()))
        launcher_config = self.home / ".config/llmwiki/icloud-wiki.sh"
        launcher_config.write_text(launcher_config.read_text().replace(str((self.runtime / "dist/cli.js").resolve()), str(self.root / "alternate cli.js")))
        subprocess.run([str(launcher), "status"], env=env, check=True, capture_output=True, text=True)
        self.assertEqual(self.node_log.read_text().splitlines()[0], str(self.root / "alternate cli.js"))

    def test_alma_launcher_uses_runtime_bridge(self):
        """The installed Alma entry point never points back into the checkout."""
        install.install(self.args)
        launcher = self.home / ".local/bin/llmwiki-alma-session"
        content = launcher.read_text()
        self.assertIn(str(self.runtime / "alma-session.py"), content)
        self.assertNotIn("deployment/alma-session.py", content)

    def test_invalid_runtime_has_no_partial_write(self):
        """Preflight rejects an incomplete runtime before creating any config."""
        self.args.runtime = str(self.root / "missing")
        with self.assertRaises(ValueError):
            install.install(self.args)
        self.assertFalse((self.home / ".config").exists())

    def test_runtime_requires_replica_support_modules(self):
        """A runtime missing replica support fails before any private file is written."""
        (self.runtime / "knowledge-flow/replica_generation.py").unlink()
        with self.assertRaisesRegex(ValueError, "required built files"):
            install.install(self.args)
        self.assertFalse((self.home / ".config").exists())

    def test_runtime_requires_context_observation_module(self):
        """An incomplete hook observation runtime fails before any private file is written."""
        (self.runtime / "knowledge-flow/context_observation.py").unlink()
        with self.assertRaisesRegex(ValueError, "required built files"):
            install.install(self.args)
        self.assertFalse((self.home / ".config").exists())

    def test_runtime_manifest_hash_mismatch_has_no_partial_write(self):
        """Preflight rejects a tampered immutable runtime before writing files."""
        target = self.runtime / "knowledge-flow/hooks.py"
        target.write_text("# intakeEnabled changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            install.install(self.args)
        self.assertFalse((self.home / ".config").exists())

    def test_exchange_roles_keep_contributor_and_publisher_separate(self):
        """Both machines collect, but only explicit designated publisher publishes."""
        self.args.config_source = str(self.exchange_source())
        self.args.machine_id = "peer-b"
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertFalse(config["intakeEnabled"])
        self.assertFalse(config["publishEnabled"])
        self.args.contributor = True
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertTrue(config["intakeEnabled"])
        self.assertFalse(config["publishEnabled"])
        self.args.contributor = False
        self.args.writer = True
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertTrue(config["intakeEnabled"])
        self.assertFalse(config["publishEnabled"])

    def test_exchange_publisher_requires_matching_identity(self):
        """Publisher activation fails closed for a non-designated participant."""
        self.args.config_source = str(self.exchange_source())
        self.args.machine_id = "peer-b"
        self.args.publisher = True
        with self.assertRaisesRegex(ValueError, "designated publisher"):
            install.install(self.args)
        self.args.machine_id = "peer-a"
        install.install(self.args)
        config = json.loads(self.config_path().read_text())
        self.assertEqual(config["machineId"], "peer-a")
        self.assertTrue(config["intakeEnabled"])
        self.assertTrue(config["publishEnabled"])

    def test_old_writer_is_demoted_when_exchange_appears(self):
        """An old writer becomes a contributor until --publisher is explicit."""
        path = self.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"enabled": True, "intakeEnabled": True, "projects": {}}))
        self.args.config_source = str(self.exchange_source())
        self.args.machine_id = "peer-a"
        install.install(self.args)
        config = json.loads(path.read_text())
        self.assertTrue(config["intakeEnabled"])
        self.assertFalse(config["publishEnabled"])

    def test_exchange_requires_participant_identity(self):
        """An exchange cannot install without a local identity in its allowlist."""
        self.args.config_source = str(self.exchange_source())
        with self.assertRaisesRegex(ValueError, "requires machineId"):
            install.install(self.args)

    def test_reinstall_preserves_pause_and_extra_exclusions(self):
        """Updating shared policy must not silently resume a paused installation."""
        install.install(self.args)
        config_path = self.config_path()
        config = json.loads(config_path.read_text())
        config.update(enabled=False, intakeEnabled=True)
        config["excludedPaths"].append(str(self.home / "private-excluded"))
        config_path.write_text(json.dumps(config))
        install.install(self.args)
        updated = json.loads(config_path.read_text())
        self.assertFalse(updated["enabled"])
        self.assertIn(str(self.home / "private-excluded"), updated["excludedPaths"])

    def test_legacy_writer_mode_is_preserved_without_role(self):
        """Legacy configs without intakeEnabled infer writer mode from our Stop hook."""
        config_path = self.config_path()
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"version": 1, "enabled": True, "projects": {}}), encoding="utf-8")
        command = install.hook_command(self.runtime, config_path)
        hooks_path = self.home / ".codex/hooks.json"
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")
        install.install(self.args)
        self.assertTrue(json.loads(config_path.read_text())["intakeEnabled"])


if __name__ == "__main__":
    unittest.main()
