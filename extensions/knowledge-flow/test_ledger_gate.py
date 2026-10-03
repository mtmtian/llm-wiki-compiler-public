"""Given/When/Then contracts for the shared knowledge-ledger activation gate.

Ledger records may only be produced once every replica participant runs a reader
(deployment/KNOWLEDGE-LEDGER.md §7.4). The gate uses the same attestation rules as
semantic topics but its own capability and shared policy, so neither activation
implies the other.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from common import save_json
from ledger_gate import CAPABILITY, activate, enabled, require_ready, status
from semantic_scope import CAPABILITY as SEMANTIC, activate as activate_semantic, apply_scope

POLICY = "exchange/v2/knowledge-ledger.json"


class LedgerGateTests(unittest.TestCase):
    """Real temporary exchange files; only the peers' announcements vary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"version": 1, "machineId": "a", "wikiRoot": str(self.root / "wiki"),
                       "stateDir": str(self.root / "state"), "node": "/node",
                       "worker": str(self.root / "runtime/knowledge-flow/worker.mjs"), "projects": {},
                       "exchange": {"protocolVersion": 2, "participants": ["a", "b"], "root": str(self.root / "exchange")}}
        save_json(self.root / "runtime/build-manifest.json", {"commit": "a" * 40, "capabilities": [SEMANTIC, CAPABILITY]})
        self.announce("a", "a")

    def announce(self, machine, commit, capabilities=(SEMANTIC, CAPABILITY)):
        save_json(self.root / f"exchange/machines/{machine}.json", {"machineId": machine, "protocolVersion": 2,
                  "runtimeCommit": commit * 40, "capabilities": list(capabilities)})

    def test_peer_without_ledger_reader_blocks_activation_without_writing(self):
        """Given a peer that only reads semantic topics, When enabling, Then no ledger policy is written."""
        self.announce("b", "b", (SEMANTIC,))
        self.assertFalse(status(self.config)["ready"])
        with self.assertRaisesRegex(ValueError, "b"):
            activate(self.config, "2026-10-03T15:00:00Z", apply=True)
        self.assertFalse((self.root / POLICY).exists())
        self.assertFalse(enabled(self.config))

    def test_ready_peers_preview_then_enable_one_shared_policy(self):
        """Given ledger readers everywhere, When previewed and applied, Then every host sees it enabled."""
        self.announce("b", "b")
        self.assertEqual(activate(self.config, "2026-10-03T15:00:00Z")["status"], "ready")
        self.assertFalse(enabled(self.config))
        self.assertEqual(activate(self.config, "2026-10-03T15:00:00Z", apply=True)["status"], "enabled")
        self.assertTrue(enabled(self.config))
        self.assertEqual(status(self.config)["knowledgeLedger"], "enabled")
        self.assertEqual(activate(self.config, "2026-10-04T15:00:00Z", apply=True)["policy"]["activatedAt"],
                         "2026-10-03T15:00:00Z")

    def test_ledger_and_semantic_activations_are_independent(self):
        self.announce("b", "b")
        activate(self.config, "2026-10-03T15:00:00Z", apply=True)
        self.assertNotIn("topicScope", apply_scope(self.config))
        activate_semantic(self.config, "2026-10-03T15:00:00Z", apply=True)
        self.assertEqual(json.loads((self.root / POLICY).read_text())["knowledgeLedger"], "enabled")

    def test_peer_downgrade_after_activation_fails_closed(self):
        """Given an enabled ledger, When a peer announces an old reader, Then producing records is blocked."""
        self.announce("b", "b")
        activate(self.config, "2026-10-03T15:00:00Z", apply=True)
        self.announce("b", "c", (SEMANTIC,))
        with self.assertRaisesRegex(ValueError, "b"):
            require_ready(self.config)

    def test_operator_cli_previews_and_enables_the_ledger(self):
        self.announce("b", "b")
        config_file = self.root / "config.json"
        save_json(config_file, self.config)
        command = [sys.executable, "-B", str(Path(__file__).with_name("maintenance.py")),
                   "--config", str(config_file), "--knowledge-ledger"]
        def invoke(*args):
            return json.loads(subprocess.run([*command, *args], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(invoke("status")["knowledgeLedger"], "disabled")
        self.assertEqual(invoke("enable")["status"], "ready")
        self.assertFalse((self.root / POLICY).exists())
        self.assertEqual(invoke("enable", "--apply")["status"], "enabled")
        self.assertEqual(invoke("status")["knowledgeLedger"], "enabled")
        refused = subprocess.run([*command, "status", "--semantic-topics", "status"], capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
