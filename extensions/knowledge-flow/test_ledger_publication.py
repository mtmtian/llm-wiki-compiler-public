"""Given/When/Then: a held batch publishes its accepted claims as one ledger record.

With the shared knowledge-ledger gate enabled (deployment/KNOWLEDGE-LEDGER.md §7.2, B3b),
the worker tells the consolidator so, and a held result may carry the claims its final
review accepted. Finalization publishes them as a version 3 record and keeps the batch
held. A closed gate or a contract violation publishes nothing and keeps the claims in
the hold. Only the replica sync and the model are scripted; the gate, baseline and shared
publication directory are real temporary files.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json, save_json
from ledger_gate import CAPABILITY, activate
from queue_worker import process_queue
from replica import initialize_baseline
from replica_records import validate_packet


def _evidence(identifier, kind, text):
    return {"id": identifier, "kind": kind, "text": text, "sha256": digest(text),
            "observedAt": "2026-10-03T00:00:00Z", "locator": "session:" + identifier}


def _claim(evidence, kind="decision", status="decided", **extra):
    return {"text": evidence["text"], "quote": evidence["text"], "evidenceId": evidence["id"], "useWhen": "always",
            "title": "t", "topic": "release", "decisionObject": "release cadence", "slug": "release-cadence",
            "targetPageId": None, "kind": kind, "status": status, "rationale": "accepted", **extra}


class LedgerPublicationTests(unittest.TestCase):
    """Drive one queued turn through prepare, a scripted model, and finalization."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state, self.exchange, shared = root / "state", root / "exchange", root / "shared"
        (shared / "wiki/concepts").mkdir(parents=True)
        (shared / "wiki/concepts/base.md").write_text("# Base", encoding="utf-8")
        save_json(root / "runtime/build-manifest.json", {"commit": "a" * 40, "capabilities": [CAPABILITY]})
        self.config = {"enabled": True, "intakeEnabled": True, "maxDailyJobs": 100, "machineId": "a",
                       "publishEnabled": True, "model": "gpt-test", "stateDir": str(self.state),
                       "worker": str(root / "runtime/knowledge-flow/worker.mjs"),
                       "wikiRoot": str(self.state / "replica/current"), "sharedWikiRoot": str(shared),
                       "projects": {"project": {"label": "Project", "pages": []}},
                       "exchange": {"protocolVersion": 2, "root": str(self.exchange), "participants": ["a"]}}
        initialize_baseline(self.config)
        save_json(self.exchange / "machines/a.json", {"machineId": "a", "protocolVersion": 2,
                  "runtimeCommit": "a" * 40, "capabilities": [CAPABILITY]})
        self.now = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
        self.user = _evidence("u", "user", "Release every Tuesday after the review")

    def enable_ledger(self):
        activate(self.config, "2026-10-03T15:00:00Z", apply=True)

    def run_held_turn(self, ledger_contribution):
        """Queue one turn whose scripted model holds the page but returns accepted claims."""
        save_json(self.state / "queue/turn.json", {"id": "turn", "projectId": "project", "projectLabel": "Project",
                  "sessionId": "s", "createdAt": "2026-10-03T00:00:00Z", "evidence": [self.user]})
        seen = []
        def invoke(config, _kind, _payload, _timeout):
            seen.append(config.get("knowledgeLedger"))
            return {"status": "needs_review", "publishedPageIds": [], "reviewCount": 1, "error": "page held",
                    "ledgerContribution": ledger_contribution}
        status = {"fullyVisibleRecordIds": [], "digest": "gen", "generationRoot": str(self.state / "g")}
        def sync(_config, after_sync=None):
            if after_sync:
                after_sync(status)
            return status
        with patch("replica.sync_replica", side_effect=sync), patch("replica.read_records", return_value=[]):
            process_queue(self.config, invoke, clock=lambda: self.now)
        audit = load_json(next((self.state / "batches").glob("*.json")))
        return seen, audit

    def publications(self):
        return [load_json(path) for path in self.exchange.glob("v2/publications/a/*.json")]

    def test_enabled_ledger_publishes_accepted_claims_and_keeps_the_hold(self):
        """Given an enabled ledger, When a held result carries accepted claims, Then one v3 record is published."""
        self.enable_ledger()
        seen, audit = self.run_held_turn({"claims": [_claim(self.user)], "evidence": [self.user]})
        self.assertEqual(seen, [True])
        self.assertEqual((audit["status"], audit["result"]["status"]), ("completed", "needs_review"))
        [packet] = self.publications()
        self.assertEqual(packet["payload"]["version"], 3)
        self.assertNotIn("topicRevisions", packet["payload"])
        validate_packet(packet, "a", packet["payload"]["baselineId"])
        self.assertEqual(audit["result"]["ledgerPublicationId"], packet["id"])
        self.assertNotIn("ledgerContribution", audit["result"])

    def test_closed_gate_publishes_nothing_and_keeps_the_claims_held(self):
        """Given no shared ledger policy, Then the worker does not ask for ledger claims and refuses any it gets."""
        seen, audit = self.run_held_turn({"claims": [_claim(self.user)], "evidence": [self.user]})
        self.assertEqual(seen, [None])
        self.assertEqual(self.publications(), [])
        self.assertEqual(audit["result"]["status"], "needs_review")
        self.assertRegex(audit["result"]["ledgerError"], "not enabled")

    def test_contract_violation_publishes_nothing_and_still_completes(self):
        """Given ledger claims that break the evidence-role contract, Then the batch completes held without them."""
        self.enable_ledger()
        assistant = _evidence("a1", "assistant", "We could release on Tuesdays")
        seen, audit = self.run_held_turn({"claims": [_claim(assistant)], "evidence": [assistant]})
        self.assertEqual(self.publications(), [])
        self.assertEqual((audit["status"], audit["result"]["status"]), ("completed", "needs_review"))
        self.assertIn("ledgerError", audit["result"])


if __name__ == "__main__":
    unittest.main()
