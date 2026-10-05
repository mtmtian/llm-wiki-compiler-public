"""Verify neutral public defaults and private configuration in isolated homes.

These tests use temporary Wiki, runtime, machine, and configuration fixtures.
They never inspect or invoke an installed host configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import install


class PublicConfigContractTests(unittest.TestCase):
    """Keep public defaults neutral while preserving explicit private setup."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.wiki = self.root / "wiki"
        self.wiki.mkdir()
        self.runtime = self.root / "runtime"
        for relative in install.RUNTIME_FILES:
            target = self.runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# intakeEnabled\n", encoding="utf-8")
        self._write_runtime_manifest()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._write_fake_binaries()
        self.env_patch = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.binary_patch = patch.object(
            install, "find_binary", side_effect=lambda name: str(self.bin / name)
        )
        self.binary_patch.start()
        self.addCleanup(self.binary_patch.stop)
        self.node_patch = patch.object(install, "check_node")
        self.node_patch.start()
        self.addCleanup(self.node_patch.stop)

    def _write_runtime_manifest(self):
        files = {name: hashlib.sha256((self.runtime / name).read_bytes()).hexdigest()
                 for name in install.RUNTIME_FILES}
        manifest = {"commit": "0123456789abcdef0123456789abcdef01234567", "files": files}
        (self.runtime / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def _write_fake_binaries(self):
        for name in ("node", "gh"):
            target = self.bin / name
            target.write_text("#!/bin/sh\nprintf 'v24.0.0\\n'\n", encoding="utf-8")
            target.chmod(0o700)

    def args(self, **overrides) -> Namespace:
        values = {"runtime": str(self.runtime), "wiki_root": str(self.wiki),
                  "config_source": None, "writer": False, "contributor": False,
                  "publisher": False, "reader": False, "machine_id": None,
                  "materializer_machine_id": None, "shared_writer_bootstrap_machine": None,
                  "dry_run": False, "event_driven": False, "disable_event_worker": False,
                  "allow_repository": []}
        values.update(overrides)
        return Namespace(**values)

    @property
    def installed_config(self) -> Path:
        return self.home / ".config/llmwiki/knowledge-flow.json"

    def test_public_template_omits_private_policy_and_preserves_existing_v2(self):
        template_path = Path(__file__).parent / "knowledge-flow.json"
        template = json.loads(template_path.read_text(encoding="utf-8"))
        for key in ("owners", "workingForks", "checks", "exchange"):
            self.assertNotIn(key, template)
        self.assertEqual(template["projects"], {})

        machine_path = self.home / ".config/llmwiki/machine.json"
        machine_path.parent.mkdir(parents=True)
        machine_path.write_text(json.dumps({"machineId": "peer-a"}), encoding="utf-8")
        exchange_root = self.wiki / ".knowledge-exchange"
        for coordinated in (False, True):
            with self.subTest(shared_writer=coordinated):
                previous = self._write_existing_v2(exchange_root, coordinated)
                install.install(self.args(writer=True, wiki_root=None))
                actual = json.loads(self.installed_config.read_text(encoding="utf-8"))
                self._assert_private_v2_preserved(actual, previous)

    def _write_existing_v2(self, exchange_root: Path, coordinated: bool):
        exchange = {"protocolVersion": 2, "root": str(exchange_root),
                    "legacyImporterMachineId": "peer-b", "materializerMachineId": "peer-b",
                    "participants": ["peer-a", "peer-b"]}
        if coordinated:
            exchange["sharedWriter"] = {"version": 1, "bootstrapMachineId": "peer-b"}
        previous = {"version": 1, "enabled": True, "machineId": "peer-a",
                    "wikiRoot": str(self.root / "stale-replica"), "sharedWikiRoot": str(self.wiki),
                    "stateDir": str(self.root / "private-state"), "model": "gpt-6-luna",
                    "owners": ["sample-owner"], "workingForks": ["sample-owner/sample-fork"],
                    "checks": [{"name": "private-check", "projectId": "sample-project"}],
                    "projects": {"sample-project": {"label": "Private sample", "paths": ["/private/project"]}},
                    "exchange": exchange, "intakeEnabled": False, "publishEnabled": False}
        self.installed_config.write_text(json.dumps(previous), encoding="utf-8")
        return previous

    def _assert_private_v2_preserved(self, config: dict, previous: dict):
        self.assertEqual(config["machineId"], "peer-a")
        for key in ("owners", "workingForks", "checks", "projects", "exchange"):
            self.assertEqual(config[key], previous[key])
        self.assertEqual(config["sharedWikiRoot"], str(self.wiki.resolve()))
        self.assertEqual(config["intakeEnabled"], True)
        self.assertEqual(config["publishEnabled"], True)

    def test_new_install_defaults_to_reader_and_dry_run_writes_nothing(self):
        preview = install.install(self.args(dry_run=True))
        self.assertTrue(preview["dryRun"])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.wiki.iterdir()), [])

        install.install(self.args())
        config = json.loads(self.installed_config.read_text(encoding="utf-8"))
        self.assertFalse(config["intakeEnabled"])
        self.assertNotIn("publishEnabled", config)
        self.assertNotIn("exchange", config)
        self.assertEqual(list(self.wiki.iterdir()), [])

    def test_writer_collects_without_exchange_and_publisher_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "--publisher requires exchange"):
            install.install(self.args(publisher=True))
        self.assertFalse(self.installed_config.exists())

        install.install(self.args(writer=True))
        config = json.loads(self.installed_config.read_text(encoding="utf-8"))
        self.assertTrue(config["intakeEnabled"])
        self.assertNotIn("publishEnabled", config)
        self.assertNotIn("exchange", config)
        self.assertEqual(list(self.wiki.iterdir()), [])

    def test_external_private_template_and_machine_overrides_are_applied(self):
        source_path = self.root / "private-flow.json"
        source = {"version": 1, "enabled": True,
                  "wikiRoot": "${HOME}/private/replica/current",
                  "stateDir": "${HOME}/private/state", "model": "gpt-6-luna",
                  "owners": ["sample-owner"], "workingForks": ["sample-owner/sample-fork"],
                  "checks": [{"name": "external-check", "projectId": "sample-project"}],
                  "projects": {"sample-project": {"label": "Sample", "paths": ["${HOME}/template/project"]}},
                  "exchange": {"protocolVersion": 2, "root": "${WIKI_ROOT}/.knowledge-exchange",
                               "legacyImporterMachineId": "peer-b", "materializerMachineId": "peer-b",
                               "participants": ["peer-a", "peer-b"]}}
        source_path.write_text(json.dumps(source), encoding="utf-8")
        local_project = self.root / "machine-project"
        machine_path = self.home / ".config/llmwiki/machine.json"
        machine_path.parent.mkdir(parents=True)
        machine_path.write_text(json.dumps({"machineId": "peer-a", "wikiRoot": str(self.wiki),
                                            "projectPaths": {"sample-project": [str(local_project)]}}),
                                encoding="utf-8")

        install.install(self.args(config_source=str(source_path), wiki_root=None, writer=True))

        actual = json.loads(self.installed_config.read_text(encoding="utf-8"))
        self.assertEqual(actual["machineId"], "peer-a")
        self.assertEqual(actual["wikiRoot"], str((self.home / "private/state/replica/current").resolve()))
        self.assertEqual(actual["sharedWikiRoot"], str(self.wiki.resolve()))
        self.assertEqual(actual["owners"], ["sample-owner"])
        self.assertEqual(actual["workingForks"], ["sample-owner/sample-fork"])
        self.assertEqual(actual["checks"], [{"name": "external-check", "projectId": "sample-project"}])
        self.assertEqual(actual["projects"]["sample-project"]["paths"], [str(local_project)])
        self.assertEqual(actual["exchange"]["participants"], ["peer-a", "peer-b"])
        self.assertEqual(actual["exchange"]["root"], str(self.wiki / ".knowledge-exchange"))
        self.assertTrue(actual["intakeEnabled"] and actual["publishEnabled"])


if __name__ == "__main__":
    unittest.main()
