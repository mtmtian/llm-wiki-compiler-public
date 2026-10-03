"""Given/When/Then contracts for reading ledger (version 3) publication records.

Ledger records carry accepted claims without page revisions; a decided claim may
supersede earlier decided claims on the same decision subject.  Readers accept
them, the current-decisions digest applies valid supersessions, and a ledger
record with any invalid reference is ignored as a whole.  Nothing here produces
ledger records; that is a later step of deployment/KNOWLEDGE-LEDGER.md §7.
"""

from __future__ import annotations

import json
import unittest

from common import digest
from current_decisions import current_decisions
from publication_resolution_contract import _validate_record_pair
from replica_records import canonical, validate_packet
from test_current_decisions import DigestFixture, claim as digest_claim
from test_topic_contract import BASELINE_ID, claim, packet
from topic_routes import _claim_for

OLD = f"{1:064x}"


def ledger(claim_value, **payload_fields):
    """Re-envelope a valid packet as a ledger record."""
    payload = packet(claim_value)["payload"]
    payload.update({"version": 3, **payload_fields})
    return {"id": digest(canonical(payload)), "payload": payload}


def superseding(*refs):
    return claim(decisionObject="首轮素材", supersedes=list(refs))


class LedgerPacketContractTests(unittest.TestCase):
    def test_ledger_record_with_supersessions_is_accepted(self):
        """Given a claims-only ledger record, When validated, Then it is accepted like a v2 record."""
        validate_packet(ledger(claim(decisionObject="首轮素材")), "a", BASELINE_ID)
        validate_packet(ledger(superseding(f"{OLD}:0", f"{OLD}:1")), "a", BASELINE_ID)

    def test_ledger_record_cannot_carry_page_revisions(self):
        with self.assertRaisesRegex(ValueError, "cannot carry topic revisions"):
            validate_packet(ledger(claim(), topicRevisions=[]), "a", BASELINE_ID)

    def test_only_ledger_decided_claims_with_a_subject_may_supersede(self):
        """Given a supersession outside its contract, When validated, Then the record is refused."""
        cases = {"v2 record": packet(superseding(f"{OLD}:0")),
                 "not decided": ledger(claim(decisionObject="首轮素材", status="historical", kind="fact",
                                             supersedes=[f"{OLD}:0"])),
                 "no decision object": ledger(claim(supersedes=[f"{OLD}:0"]))}
        for name, value in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "supersedes is invalid"):
                validate_packet(value, "a", BASELINE_ID)

    def test_malformed_supersede_references_are_refused(self):
        for refs in ("x", [], [f"{OLD}:0", f"{OLD}:0"], [f"{OLD}:01"], ["abc:0"], [7], [f"{OLD}:0"] * 6):
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, "supersedes is invalid"):
                validate_packet(ledger(claim(decisionObject="首轮素材", supersedes=refs)), "a", BASELINE_ID)

    def test_unknown_versions_stay_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown publication version"):
            validate_packet(ledger(claim(), version=4), "a", BASELINE_ID)


class LedgerDigestTests(DigestFixture):
    """Run the real digest over cached, fully visible records."""

    def ledger(self, number, created, claims, **options):
        self.publish(number, created, claims, **options)
        path = self.state / "replica-records" / f"{number:064x}.json"
        record = json.loads(path.read_text())
        record["payload"]["version"] = 3
        path.write_text(json.dumps(record, ensure_ascii=False))

    def newer(self, refs=(f"{OLD}:0",), subject="投放结构"):
        return {**digest_claim("新规则", subject=subject), "supersedes": list(refs)}

    def test_valid_supersession_hides_the_older_decision(self):
        """Given a ledger decision that supersedes an older one, When the digest renders, Then only the new one shows."""
        self.publish(1, "2026-09-20T00:00:00Z", [digest_claim("旧规则"), digest_claim("仍有效")])
        self.ledger(2, "2026-09-25T00:00:00Z", [self.newer()])
        text, _ = current_decisions(self.config, "growth")
        self.assertIn("2026-09-25 决定：新规则", text)
        self.assertIn("仍有效", text)
        self.assertNotIn("旧规则", text)

    def test_any_invalid_reference_rejects_the_whole_ledger_record(self):
        """Given an invalid reference, When the digest renders, Then the ledger record neither shows nor hides."""
        cases = {"other subject": ({}, {"subject": "素材"}), "later target": ({"created": "2026-09-26T00:00:00Z"}, {}),
                 "not decided": ({"status": "historical"}, {}), "invisible target": ({"visible": False}, {}),
                 "other project": ({"project": "other"}, {}),
                 "unknown index": ({}, {"refs": (f"{OLD}:0", f"{OLD}:5")})}
        for name, (target, newer) in cases.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                status = target.pop("status", "decided")
                self.publish(1, target.pop("created", "2026-09-20T00:00:00Z"), [digest_claim("旧规则", status=status)], **target)
                self.ledger(2, "2026-09-25T00:00:00Z", [self.newer(**newer)])
                text, _ = current_decisions(self.config, "growth")
                self.assertNotIn("新规则", text)
                self.assertEqual("旧规则" in text, status == "decided" and name not in ("invisible target", "other project"))


class LedgerExclusionTests(unittest.TestCase):
    """Paths built for page claims never treat a ledger record as one."""

    def test_topic_routes_cannot_target_a_ledger_claim(self):
        record = ledger(claim())
        with self.assertRaisesRegex(ValueError, "legacy claim"):
            _claim_for(f"{record['id']}:0", {record["id"]: record})

    def test_a_ledger_record_is_not_a_legacy_record_to_resolve(self):
        old = ledger(claim())["payload"]
        replacement = {**packet(claim())["payload"], "topicRevisions": [{"pageId": "concepts/x"}]}
        with self.assertRaisesRegex(ValueError, "legacy record"):
            _validate_record_pair(old, replacement, None)


if __name__ == "__main__":
    unittest.main()
