"""Given/When/Then contracts for reviewed claim projections in replica generations.

The projection keeps accepted v3 claims discoverable before topic pages exist,
while preserving superseded history and excluding records held by materializer
conflicts.  It is a sealed read view, not a second publication protocol.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from common import digest
from replica import publish_record, read_baseline, sync_replica
from replica_generation import _digest_for
from replica_integrity import read_verified_generation, seal_generation
from replica_records import canonical, validate_packet
import test_replica
from test_topic_contract import BASELINE_ID, claim, packet


GENERATION = "g" * 64
SUBJECT = "样例素材推广"


def _record(claim_value, version=3, created_at="2026-09-17T00:00:00Z", **metadata):
    """Build one deterministic packet from the existing topic-contract fixture."""
    value = packet(copy.deepcopy(claim_value))
    payload = value["payload"]
    payload.update({"version": version, "createdAt": created_at, **metadata})
    return {"id": digest(canonical(payload)), "payload": payload}


def _ledger_record(claim_value, created_at, baseline_id, **metadata):
    """Build a v3 record with the real fixture's accepted evidence envelope."""
    return _record(claim_value, 3, created_at, baselineId=baseline_id, **metadata)


def _claim_text(projection, text):
    """Find one rendered claim by its public statement."""
    return next(item for item in projection["claims"] if item["text"] == text)


class LedgerProjectionTests(unittest.TestCase):
    """Exercise deterministic claim projection and its generation integration."""

    def test_v3_without_page_projects_exact_evidence(self):
        """Given a verified ledger claim without a page, Then its exact quote is addressable."""
        from ledger_projection import build_reviewed_claims_projection

        source = claim(text="Keep the exact source", quote="Keep the exact source")
        record = _record(source, projectId="project", projectLabel="Project")
        projection = build_reviewed_claims_projection([record], [], GENERATION)

        item = projection["claims"][0]
        quote = item["quotes"][0]
        self.assertIsNone(item["targetPageId"])
        self.assertEqual(item["claimRef"], f"{record['id']}:0")
        self.assertEqual(quote["quote"], "Keep the exact source")
        self.assertEqual(quote["locator"], record["payload"]["evidence"][0]["locator"])
        self.assertEqual(quote["sha256"], digest(quote["quote"]))
        self.assertEqual(quote["originalSha256"], record["payload"]["evidence"][0]["originalSha256"])

    def test_supporting_quotes_follow_primary_and_deduplicate_evidence_ids(self):
        """Given repeated supporting evidence, Then the primary leads and each id appears once."""
        from ledger_projection import build_reviewed_claims_projection

        value = claim(text="Keep primary context", quote="Keep primary context")
        value["supportingQuotes"] = [{"evidenceId": "e2", "quote": "Supporting context"}] * 2
        record = packet(value)
        evidence = {"id": "e2", "kind": "artifact", "text": "Supporting context",
                    "sha256": digest("Supporting context"), "originalSha256": digest("original support"),
                    "observedAt": "2026-09-17T01:00:00Z",
                    "locator": "knowledge-evidence://a/" + digest("turn:e2")}
        record["payload"]["version"] = 3
        record["payload"]["evidence"].append(evidence)
        record["id"] = digest(canonical(record["payload"]))
        validate_packet(record, "a", BASELINE_ID)

        projected = build_reviewed_claims_projection([record], [], GENERATION)["claims"][0]

        self.assertEqual([item["evidenceId"] for item in projected["quotes"]], ["e1", "e2"])
        self.assertEqual(projected["quotes"][1]["sha256"], evidence["sha256"])

    def test_materializer_conflicts_never_leak_claims(self):
        """Given a conflict record id, When projected, Then all its claims are rejected."""
        from ledger_projection import build_reviewed_claims_projection

        blocked = _record(claim(text="Held statement", quote="Held statement"))
        visible = _record(claim(text="Accepted statement", quote="Accepted statement"))
        projection = build_reviewed_claims_projection(
            [blocked, visible], [{"recordIds": [blocked["id"]]}], GENERATION)

        self.assertEqual([item["text"] for item in projection["claims"]], ["Accepted statement"])
        self.assertIn(blocked["id"], projection["rejectedRecordIds"])

    def test_valid_supersession_keeps_all_v3_and_exposes_replaced_v2(self):
        """Given valid replacements, Then v3 history stays readable and replaced v2 metadata remains."""
        from ledger_projection import build_reviewed_claims_projection

        old_v2 = _record(claim(decisionObject=SUBJECT), 2, "2026-09-10T00:00:00Z")
        old_v3 = _record(claim(decisionObject=SUBJECT, text="Earlier accepted rule",
                               quote="Earlier accepted rule"), 3, "2026-09-12T00:00:00Z")
        newest = _record(claim(decisionObject=SUBJECT, text="Current accepted rule",
                               quote="Current accepted rule",
                               supersedes=[f"{old_v2['id']}:0", f"{old_v3['id']}:0"]),
                          3, "2026-09-20T00:00:00Z")
        projection = build_reviewed_claims_projection([newest, old_v3, old_v2], [], GENERATION)

        self.assertTrue(_claim_text(projection, "Earlier accepted rule")["superseded"])
        self.assertFalse(_claim_text(projection, "Current accepted rule")["superseded"])
        self.assertEqual([item["recordId"] for item in projection["superseded"]], [old_v2["id"]])
        self.assertNotIn("Earlier accepted rule", [item["text"] for item in projection["superseded"]])

    def test_invalid_supersedes_rejects_the_entire_v3_record(self):
        """Given one invalid supersede reference, Then every claim in that record is omitted."""
        from ledger_projection import build_reviewed_claims_projection

        invalid = _record(claim(decisionObject=SUBJECT, supersedes=[f"{BASELINE_ID}:9"]))
        invalid["payload"]["claims"].append(claim(text="Same record second claim", quote="Same record second claim"))
        invalid["id"] = digest(canonical(invalid["payload"]))
        valid = _record(claim(text="Independent claim", quote="Independent claim"))
        projection = build_reviewed_claims_projection([invalid, valid], [], GENERATION)

        self.assertEqual([item["text"] for item in projection["claims"]], ["Independent claim"])
        self.assertIn(invalid["id"], projection["rejectedRecordIds"])

    def test_ordinary_v2_claim_is_not_projected(self):
        """Given an ordinary page-backed v2 record, Then it creates no standalone claim entry."""
        from ledger_projection import build_reviewed_claims_projection

        legacy = _record(claim(), 2)
        projection = build_reviewed_claims_projection([legacy], [], GENERATION)

        self.assertEqual(projection["claims"], [])
        self.assertEqual(projection["superseded"], [])

    def test_input_order_does_not_change_projection(self):
        """Given identical records in opposite orders, Then serialized projections match."""
        from ledger_projection import build_reviewed_claims_projection

        records = [_record(claim(text=f"Rule {index}", quote=f"Rule {index}"),
                           created_at=f"2026-09-{17 + index:02d}T00:00:00Z") for index in range(3)]
        first = build_reviewed_claims_projection(records, [], GENERATION)
        reversed_input = build_reviewed_claims_projection(list(reversed(records)), [], GENERATION)

        self.assertEqual(first, reversed_input)

    def test_projection_version_changes_generation_identity(self):
        """Given a new read projection version, Then the generation id cannot reuse old bytes."""
        from replica_generation import READ_PROJECTION_VERSION as current_version

        records = [_record(claim())]
        initial = _digest_for(BASELINE_ID, records)
        with patch("replica_generation.READ_PROJECTION_VERSION", current_version + 1):
            upgraded = _digest_for(BASELINE_ID, records)

        self.assertNotEqual(initial, upgraded)

    def test_sync_writes_projection_before_real_seal_and_seal_detects_changes(self):
        """Given a synced v3 claim, Then projection bytes are sealed and tampering fails closed."""
        fixture = test_replica.ReplicaTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = fixture.configs["a"]
        baseline_id = read_baseline(config)["snapshotId"]
        job, result = fixture._submission("a", "Older reviewed statement", "when relevant", "old-v2")
        result["contribution"]["claims"][0].update(topic=SUBJECT, decisionObject=SUBJECT)
        old = publish_record(config, job, result)["publicationId"]
        latest = _ledger_record(claim(decisionObject=SUBJECT, supersedes=[f"{old}:0"]),
                                "2026-09-20T00:00:00Z", baseline_id,
                                machineId="a", projectId="project", projectLabel="Project")
        validate_packet(latest, "a", baseline_id)
        _write_packet(config, latest)

        def assert_projection_precedes_seal(stage, generation_id):
            self.assertTrue((Path(stage) / ".llmwiki/reviewed-claims.json").is_file())
            return seal_generation(stage, generation_id)

        with patch("replica_generation.seal_generation", side_effect=assert_projection_precedes_seal):
            status = sync_replica(config, lambda _stage, _records: {"pages": 0, "conflicts": []})
        generation = Path(status["generationRoot"])
        projection_file = generation / ".llmwiki/reviewed-claims.json"
        read_verified_generation(generation)
        self.assertEqual(json.loads(projection_file.read_text(encoding="utf-8"))["claims"][0]["text"],
                         latest["payload"]["claims"][0]["text"])
        manifest = json.loads((generation / ".llmwiki/projection-manifest.json").read_text(encoding="utf-8"))
        self.assertIn(".llmwiki/reviewed-claims.json", {item["path"] for item in manifest["consumerFiles"]})
        projection_file.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            read_verified_generation(generation)


def _write_packet(config, value):
    """Place one synthetic packet in the existing replica integration fixture."""
    directory = Path(config["exchange"]["root"]) / "v2/publications/a"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{value['id']}.json").write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
