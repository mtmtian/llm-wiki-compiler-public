"""Isolation tests for event worker preparation and launchd lifecycle."""

from __future__ import annotations

import plistlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import install
import install_events as event_module
from install_events import (EVENT_LABEL, directories_to_create, event_paths,
                            event_enabled, event_plan, launchctl_action, remove_owned_files,
                            watch_paths)


class EventInstallTests(unittest.TestCase):
    """Keep event activation narrow, explicit, and role-aware."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home with spaces"
        self.runtime = self.root / "runtime with spaces"
        self.config_path = self.home / ".config/llmwiki/knowledge-flow.json"
        self.exchange = self.root / "shared exchange"
        self.codex = self.root / "codex bin with spaces" / "codex"
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.codex.chmod(0o700)
        self.node = self.root / "node bin with spaces" / "node"
        self.node.parent.mkdir(parents=True)
        original_which = shutil.which
        self.which_patch = patch.object(
            event_module.shutil, "which",
            side_effect=lambda name: str(self.codex) if name == "codex" else original_which(name),
        )
        self.which_patch.start()
        self.config = {
            "enabled": True, "intakeEnabled": True, "eventDriven": {"enabled": True},
            "node": str(self.node),
            "stateDir": str(self.root / "state dir"),
            "exchange": {"root": str(self.exchange), "publisherMachineId": "peer-a",
                          "participants": ["peer-a", "peer-b"]},
            "machineId": "peer-b", "publishEnabled": False,
        }

    def tearDown(self):
        self.which_patch.stop()
        self.temp.cleanup()

    def test_contributor_watches_intake_and_receipts_only(self):
        """A contributor cannot watch exchange roots or its own submissions."""
        paths = [str(path) for path in watch_paths(self.config)]
        self.assertEqual(paths, [str((Path(self.config["stateDir"]) / "queue").resolve()),
                                 str((Path(self.config["stateDir"]) / "capture-pending").resolve()),
                                 str((self.exchange / "receipts").resolve())])
        self.assertNotIn(str(self.exchange), paths)
        self.assertNotIn("peer-b", " ".join(paths))

    def test_publisher_watches_each_participant_submission(self):
        """Only a designated publisher receives participant submission signals."""
        config = {**self.config, "machineId": "peer-a", "publishEnabled": True}
        paths = [str(path) for path in watch_paths(config)]
        self.assertEqual(paths[0], str((Path(config["stateDir"]) / "queue").resolve()))
        self.assertEqual(paths[1], str((Path(config["stateDir"]) / "capture-pending").resolve()))
        self.assertEqual(paths[2:], [str((self.exchange / "submissions/peer-a").resolve()),
                                     str((self.exchange / "submissions/peer-b").resolve())])

    def test_v2_watches_publications_and_baseline_without_replica_status(self):
        """v2 watches immutable shared signals, never its generated local view."""
        config = {**self.config, "exchange": {"protocolVersion": 2, "root": str(self.exchange),
                                                "participants": ["peer-a", "peer-b"]}}
        paths = [str(path) for path in watch_paths(config)]
        self.assertEqual(paths, [str((Path(config["stateDir"]) / "queue").resolve()),
                                 str((Path(config["stateDir"]) / "capture-pending").resolve()),
                                 str((self.exchange / "v2/publications").resolve()),
                                 str((self.exchange / "v2/publications/peer-a").resolve()),
                                 str((self.exchange / "v2/publications/peer-b").resolve()),
                                 str((self.exchange / "v2/baseline.json").resolve())])
        self.assertNotIn("replica/current", " ".join(paths))
        self.assertNotIn("status", " ".join(paths))

    def test_v2_precreates_participant_publication_directories_only(self):
        """Preparation creates publication parents but never invents a baseline."""
        config = {**self.config, "exchange": {"protocolVersion": 2, "root": str(self.exchange),
                                                "participants": ["peer-a", "peer-b"]}}
        directories = directories_to_create(config)
        self.assertIn((self.exchange / "v2/publications").resolve(), directories)
        self.assertIn((self.exchange / "v2/publications/peer-a").resolve(), directories)
        self.assertNotIn((self.exchange / "v2/baseline.json").resolve(), directories)

    def test_coordinated_wake_handles_requests_and_receipts_without_a_conversation(self):
        """Given coordination, launchd watches both issuers and retains timed fallback."""
        from install_events import render_plist
        config = {**self.config, 'exchange': {'protocolVersion': 2, 'root': str(self.exchange),
                  'participants': ['peer-a', 'peer-b'],
                  'sharedWriter': {'version': 1, 'bootstrapMachineId': 'peer-b'}}}
        plist = plistlib.loads(render_plist(config, self.home / 'wake'))
        for directory in ('requests', 'releases'):
            for machine in config['exchange']['participants']:
                path = (self.exchange / 'v2/shared-writer' / directory / machine).resolve()
                self.assertIn(str(path), plist['WatchPaths'])
                self.assertIn(path, directories_to_create(config))
        self.assertTrue(plist['RunAtLoad'])
        self.assertEqual(plist['StartCalendarInterval'], [{'Minute': x} for x in range(0, 60, 5)])

    def test_launcher_exports_codex_and_node_directories_for_launchd(self):
        """The non-interactive launcher resolves codex despite launchd's minimal PATH."""
        wake = self.runtime / "knowledge-flow/wake.py"
        wake.parent.mkdir(parents=True)
        wake.write_text("import shutil,sys\npath=shutil.which('codex')\n"
                        "print(path or 'missing', file=sys.stderr if not path else sys.stdout)\n"
                        "raise SystemExit(3 if not path else 0)\n", encoding="utf-8")
        paths, launcher, _, _ = event_plan(self.config, self.home, self.runtime,
                                            sys.executable, self.config_path)
        paths.launcher.parent.mkdir(parents=True)
        paths.launcher.write_text(launcher, encoding="utf-8")
        paths.launcher.chmod(0o700)
        result = subprocess.run(
            [str(paths.launcher)], env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), str(self.codex))
        self.assertIn(str(self.node.parent), launcher)

    def test_discovered_codex_directory_precedes_configured_node_directory(self):
        """When both directories contain codex, launchd resolves the discovered one."""
        chosen = self.root / "chosen codex with spaces" / "codex"
        chosen.parent.mkdir(parents=True)
        chosen.write_text("#!/bin/sh\nprintf chosen\n", encoding="utf-8")
        chosen.chmod(0o700)
        shadow = self.node.parent / "codex"
        shadow.write_text("#!/bin/sh\nprintf shadow\n", encoding="utf-8")
        shadow.chmod(0o700)
        wake = self.runtime / "knowledge-flow/wake.py"
        wake.parent.mkdir(parents=True)
        wake.write_text("import subprocess\nprint(subprocess.check_output(['codex'], text=True).strip())\n",
                        encoding="utf-8")
        with patch.object(event_module.shutil, "which", return_value=str(chosen)):
            paths, launcher, _, _ = event_plan(self.config, self.home, self.runtime,
                                                sys.executable, self.config_path)
        paths.launcher.parent.mkdir(parents=True)
        paths.launcher.write_text(launcher, encoding="utf-8")
        paths.launcher.chmod(0o700)
        result = subprocess.run([str(paths.launcher)], env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "chosen")

    def test_old_launcher_fails_when_launchd_path_lacks_codex(self):
        """The pre-fix launcher cannot resolve codex under launchd's system PATH."""
        wake = self.runtime / "knowledge-flow/wake.py"
        wake.parent.mkdir(parents=True)
        wake.write_text("import shutil,sys\n"
                        "raise SystemExit(0 if shutil.which('codex') else 3)\n", encoding="utf-8")
        old = "\n".join(("#!/bin/sh", "set -eu", f"exec {shlex.quote(sys.executable)} "
                           f"{shlex.quote(str(wake))} --config {shlex.quote(str(self.config_path))}", ""))
        launcher = self.root / "old launcher"
        launcher.write_text(old, encoding="utf-8")
        launcher.chmod(0o700)
        result = subprocess.run([str(launcher)], env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}, check=False)
        self.assertEqual(result.returncode, 3)

    def test_writer_preparation_fails_without_codex(self):
        """Model intake cannot be prepared when the codex executable is absent."""
        with patch.object(event_module.shutil, "which", return_value=None), self.assertRaisesRegex(
                ValueError, "requires executable codex"):
            event_plan(self.config, self.home, self.runtime, sys.executable, self.config_path)

    def test_reader_sync_preparation_does_not_require_codex(self):
        """A v2 reader can prepare no-model synchronization without codex installed."""
        config = {**self.config, "intakeEnabled": False,
                  "exchange": {"protocolVersion": 2, "root": str(self.exchange),
                                "participants": ["peer-a", "peer-b"]}}
        with patch.object(event_module.shutil, "which", return_value=None):
            self.assertTrue(event_enabled(config))
            event_plan(config, self.home, self.runtime, sys.executable, self.config_path)

    def test_plist_has_no_hot_restart_or_queue_directories(self):
        """RunAtLoad and calendar ticks provide recovery without hot-restart keys."""
        _, _, raw, _ = event_plan(self.config, self.home, self.runtime, "/usr/bin/python3", self.config_path)
        plist = plistlib.loads(raw)
        self.assertEqual(plist["Label"], EVENT_LABEL)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StartCalendarInterval"],
                         [{"Minute": minute} for minute in range(0, 60, 5)])
        self.assertEqual(len(plist["StartCalendarInterval"]), 12)
        self.assertNotIn("KeepAlive", plist)
        self.assertNotIn("QueueDirectories", plist)
        self.assertNotIn("StartInterval", plist)

    def test_dry_run_never_invokes_launchctl(self):
        """Dry-run reports the exact command without requiring launchctl."""
        result = launchctl_action("enable", self.root / "worker.plist", dry_run=True,
                                 executable="/missing/fake-launchctl", platform_name="Darwin", uid=42)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["command"][-1], str(self.root / "worker.plist"))

    def test_status_reports_fake_launchctl_result(self):
        """Lifecycle status is based on launchctl's return code and output."""
        fake = self.root / "fake launchctl"
        fake.write_text("#!/bin/sh\nprintf 'service loaded\\n'\n", encoding="utf-8")
        fake.chmod(0o700)
        result = launchctl_action("status", self.root / "worker.plist", executable=str(fake),
                                 platform_name="Darwin", uid=42)
        self.assertTrue(result["active"])
        self.assertIn("service loaded", result["stdout"])

    def test_mutations_are_verified_against_fake_service_state(self):
        """Bootstrap and bootout report the service state, not command exit alone."""
        state = self.root / "loaded"
        fake = self.root / "stateful launchctl"
        fake.write_text("#!/bin/sh\n"
                        "case \"$1\" in\n"
                        "bootstrap) touch '" + str(state) + "'; exit 0;;\n"
                        "bootout) rm -f '" + str(state) + "'; exit 0;;\n"
                        "print) if test -f '" + str(state) + "'; then exit 0; else echo 'Could not find service' >&2; exit 113; fi;;\n"
                        "esac\n", encoding="utf-8")
        fake.chmod(0o700)
        enabled = launchctl_action("enable", self.root / "worker.plist", executable=str(fake),
                                  platform_name="Darwin", uid=42)
        self.assertTrue(enabled["success"])
        self.assertTrue(enabled["active"])
        stopped = launchctl_action("bootout", self.root / "worker.plist", executable=str(fake),
                                   platform_name="Darwin", uid=42)
        self.assertTrue(stopped["success"])
        self.assertFalse(stopped["active"])

    def test_bootstrap_error_is_not_hidden_by_active_status(self):
        """An active service does not turn an unrelated bootstrap failure into success."""
        fake = self.root / "bootstrap failure launchctl"
        fake.write_text("#!/bin/sh\n"
                        "if [ \"$1\" = bootstrap ]; then echo 'Input/output error' >&2; exit 1; fi\n"
                        "exit 0\n", encoding="utf-8")
        fake.chmod(0o700)
        result = launchctl_action("enable", self.root / "worker.plist", executable=str(fake),
                                  platform_name="Darwin", uid=42)
        self.assertFalse(result["success"])
        self.assertTrue(result["active"])

    def test_rollback_removes_only_owned_files_and_keeps_queue(self):
        """Rollback cannot erase queue evidence or state directories."""
        paths, launcher, plist, directories = event_plan(self.config, self.home, self.runtime,
                                                          "/usr/bin/python3", self.config_path)
        paths.launcher.parent.mkdir(parents=True)
        paths.plist.parent.mkdir(parents=True)
        paths.launcher.write_text(launcher, encoding="utf-8")
        paths.plist.write_bytes(plist)
        queue = Path(self.config["stateDir"]) / "queue"
        queue.mkdir(parents=True)
        (queue / "job.json").write_text("{}", encoding="utf-8")
        removed = remove_owned_files(paths)
        self.assertEqual(set(removed), {str(paths.launcher), str(paths.plist)})
        self.assertTrue((queue / "job.json").exists())
        self.assertIn(queue.resolve(), directories)
        self.assertIn((Path(self.config["stateDir"]) / "reports").resolve(), directories)
        self.assertIn((self.exchange / "submissions/peer-b").resolve(), directories)

    def test_failed_bootout_keeps_owned_files_for_retry(self):
        """Given an active Mac service, failed bootout preserves files for a later retry."""
        paths = event_paths(self.home, self.config_path)
        paths.launcher.parent.mkdir(parents=True)
        paths.plist.parent.mkdir(parents=True)
        paths.launcher.write_text("launcher", encoding="utf-8")
        paths.plist.write_text("plist", encoding="utf-8")
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        fake = self.root / "failing launchctl"
        fake.write_text("#!/bin/sh\n"
                        "if [ \"$1\" = print ]; then exit 0; fi\n"
                        "exit 1\n", encoding="utf-8")
        fake.chmod(0o700)
        with patch.dict(os.environ, {"HOME": str(self.home)}), \
                patch.object(event_module.platform, "system", return_value="Darwin"):
            result = install.event_worker(Namespace(event_worker_action="rollback", dry_run=False,
                                                     event_worker_config=str(self.config_path),
                                                     event_worker_launchctl=str(fake)))
        self.assertFalse(result["success"])
        self.assertTrue(result["supported"])
        self.assertTrue(result["active"])
        self.assertFalse(result["confirmedInactive"])
        self.assertEqual(result["removed"], [])
        self.assertIn("rollbackBlocked", result)
        self.assertEqual(paths.launcher.read_text(), "launcher")
        self.assertEqual(paths.plist.read_text(), "plist")

    def test_unsupported_platform_cannot_remove_owned_files(self):
        """Given Linux, rollback reports unsupported and preserves the prepared files."""
        paths = event_paths(self.home, self.config_path)
        for target in (paths.launcher, paths.plist):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("owned", encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(self.home)}), \
                patch.object(event_module.platform, "system", return_value="Linux"):
            result = install.event_worker(Namespace(event_worker_action="rollback", dry_run=False,
                                                     event_worker_config=str(self.config_path),
                                                     event_worker_launchctl="/missing/launchctl"))
        self.assertFalse(result["supported"])
        self.assertEqual(result["removed"], [])
        self.assertIn("rollbackBlocked", result)
        self.assertEqual(paths.launcher.read_text(), "owned")
        self.assertEqual(paths.plist.read_text(), "owned")

    def test_recovery_does_not_require_readable_configuration(self):
        """Missing or corrupt config cannot prevent stopping and removing the owned service."""
        paths = event_paths(self.home, self.config_path)
        queue = Path(self.config["stateDir"]) / "queue/job.json"
        queue.parent.mkdir(parents=True)
        queue.write_text("private evidence")
        self.config_path.parent.mkdir(parents=True)
        for content in (None, "{broken"):
            for action in ("bootout", "status", "rollback"):
                with self.subTest(content=content, action=action):
                    self.config_path.unlink(missing_ok=True)
                    if content is not None:
                        self.config_path.write_text(content)
                    for target in (paths.launcher, paths.plist):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("owned")
                    args = Namespace(event_worker_action=action, dry_run=False,
                                     event_worker_config=str(self.config_path), event_worker_launchctl=None)
                    with patch.object(install, "home_dir", return_value=self.home), \
                            patch.object(install, "launchctl_action", return_value={"confirmedInactive": True}) as ctl:
                        result = install.event_worker(args)
                    self.assertEqual(ctl.call_args.args[0], "bootout" if action == "rollback" else action)
                    self.assertEqual(queue.read_text(), "private evidence")
                    self.assertEqual(paths.plist.exists(), action != "rollback")
                    if action == "rollback":
                        self.assertEqual(set(result["removed"]), {str(paths.launcher), str(paths.plist)})


if __name__ == "__main__":
    unittest.main()
