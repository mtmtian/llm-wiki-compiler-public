"""Given/When/Then tests for topic revision and full-vault migration wire data."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import digest
from replica import _publication_payload, read_baseline
from replica_records import canonical, validate_packet
from revision_contract import validate_topic_migration, validate_topic_revisions
from topic_routes import load_topic_projection, validate_topic_routes


BASELINE = "b" * 64


def _evidence(identifier: str, text: str) -> dict[str, str]:
    """Build an opaque, hash-consistent user evidence item."""
    return {"id": identifier, "kind": "user", "text": text, "sha256": digest(text),
            "originalSha256": digest("original:" + text), "observedAt": "2026-09-17T00:00:00Z",
            "locator": "knowledge-evidence://a/" + digest(identifier)}


def _packet() -> dict[str, object]:
    """Build two claims sharing one reviewed topic destination."""
    evidence = [_evidence("e1", "保留视频访问"), _evidence("e2", "下周复盘转化"), _evidence("e3", "预算先小额")]
    claims = [
        {"text": evidence[0]["text"], "evidenceId": "e1", "quote": evidence[0]["text"], "title": "投放决策",
         "topic": "投放", "decisionObject": "样例首轮素材", "slug": "video", "targetPageId": "concepts/sample-material-test.md",
         "kind": "decision", "status": "decided", "useWhen": "首轮", "rationale": "验证访问", "supportingQuotes": [{"evidenceId": "e3", "quote": evidence[2]["text"]}]},
        {"text": evidence[1]["text"], "evidenceId": "e2", "quote": evidence[1]["text"], "title": "投放决策",
         "topic": "投放", "decisionObject": "样例首轮素材", "slug": "video", "targetPageId": "concepts/sample-material-test.md",
         "kind": "decision", "status": "decided", "useWhen": "复盘", "rationale": "观察转化"},
    ]
    payload = {"version": 2, "baselineId": BASELINE, "machineId": "a", "projectId": "demo",
               "projectLabel": "Demo", "createdAt": "2026-09-17T00:00:00Z", "originJobHash": "c" * 64,
               "repoIdentity": None, "basisRecordIds": [], "claims": claims, "evidence": evidence,
               "topicRevisions": [{"pageId": "concepts/sample-material-test.md", "topicId": "d" * 64, "title": "投放决策",
                                    "topic": "投放", "decisionObject": "样例首轮素材", "basisHash": None,
                                    "body": "# 投放\n\n{{claim:0}}\n{{claim:1}}", "claimIndexes": [0, 1]}],
               "review": {"status": "accepted", "model": "test"}}
    return {"id": digest(canonical(payload)), "payload": payload}


class RevisionWireTests(unittest.TestCase):
    """Topic revisions and migrations fail closed before a generation switch."""

    def test_multi_evidence_topic_revision_is_exactly_covered(self):
        """Given a page revision, When its packet is validated, Then all quotes survive."""
        packet = _packet()
        validate_packet(packet, "a", BASELINE)
        self.assertEqual(len(packet["payload"]["evidence"]), 3)

    def test_assistant_support_can_extend_a_user_decision(self):
        """Given user confirmation plus assistant context, When exported, Then both remain valid evidence."""
        packet = _packet()
        packet["payload"]["evidence"][2]["kind"] = "assistant"
        packet["id"] = digest(canonical(packet["payload"]))
        validate_packet(packet, "a", BASELINE)

    def test_export_then_packet_validation_keeps_assistant_context(self):
        """Given a submitted contribution, When exported and revalidated, Then supporting context remains."""
        import test_replica
        fixture = test_replica.ReplicaTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.configs["a"]["projects"]["demo"] = {"label": "Demo", "pages": []}
        source = _packet()["payload"]
        result = {"status": "submitted", "contribution": {key: source[key] for key in ("claims", "evidence", "topicRevisions")}}
        job = {"id": "export-job", "projectId": "demo", "projectLabel": "Demo",
               "createdAt": "2026-09-17T00:00:00Z", "basisRecordIds": []}
        payload = _publication_payload(fixture.configs["a"], job, result, read_baseline(fixture.configs["a"])["snapshotId"])
        validate_packet({"id": digest(canonical(payload)), "payload": payload}, "a", payload["baselineId"])

    def test_assistant_primary_cannot_become_a_decision(self):
        """Given only assistant support for a decision, When validated, Then it is rejected."""
        packet = _packet()
        packet["payload"]["claims"][0]["evidenceId"] = "e3"
        packet["payload"]["claims"][0]["quote"] = packet["payload"]["evidence"][2]["text"]
        packet["payload"]["claims"][0]["text"] = packet["payload"]["evidence"][2]["text"]
        packet["payload"]["evidence"][2]["kind"] = "assistant"
        packet["id"] = digest(canonical(packet["payload"]))
        with self.assertRaisesRegex(ValueError, "assistant"):
            validate_packet(packet, "a", BASELINE)

    def test_unknown_placeholder_is_rejected_in_reverse_validation(self):
        """Given a malformed body, When validation runs, Then an unknown claim is rejected."""
        packet = _packet()
        packet["payload"]["topicRevisions"][0]["body"] = "{{claim:0}} {{claim:9}}"
        with self.assertRaisesRegex(ValueError, "placeholder"):
            packet["id"] = digest(canonical(packet["payload"]))
            validate_packet(packet, "a", BASELINE)

    def test_topic_id_survives_display_title_revision(self):
        """Given a stable topic id, When its title is clarified, Then identity remains valid."""
        packet = _packet()
        revision = packet["payload"]["topicRevisions"][0]
        revision["title"] = "样例首轮素材推广决策"
        packet["id"] = digest(canonical(packet["payload"]))
        validate_packet(packet, "a", BASELINE)

    def test_duplicate_revision_page_is_rejected(self):
        """Given two revisions for one page, When validated, Then destinations remain one-to-one."""
        packet = _packet()
        revision = copy.deepcopy(packet["payload"]["topicRevisions"][0])
        revision["claimIndexes"] = [0]
        revision["body"] = "{{claim:0}}"
        packet["payload"]["topicRevisions"] = [packet["payload"]["topicRevisions"][0], revision]
        packet["id"] = digest(canonical(packet["payload"]))
        with self.assertRaisesRegex(ValueError, "page"):
            validate_packet(packet, "a", BASELINE)

    def test_v2_migration_requires_known_basis_and_preserves_previous_pages(self):
        """Given a reviewed migration, When validated, Then old page hashes remain attached."""
        packet = _packet()
        migration = {"version": 1, "basisRecordIds": [packet["id"]], "pages": [{
            "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md", "topicId": "d" * 64,
            "title": "投放决策", "topic": "投放", "decisionObject": "样例首轮素材", "body": "# 投放",
            "previousPages": [{"pageId": "concepts/old.md", "sha256": "e" * 64}],
        }]}
        validate_topic_migration(migration, BASELINE, [packet])
        manifest = {"version": 2, "baselineId": BASELINE, "reviewedAt": "2026-09-17T00:00:00Z",
                    "groups": [], "migration": migration}
        self.assertEqual(validate_topic_routes(manifest, BASELINE, [packet]), [])
        broken = copy.deepcopy(migration)
        broken["basisRecordIds"] = ["f" * 64]
        with self.assertRaisesRegex(ValueError, "basis"):
            validate_topic_migration(broken, BASELINE, [packet])

    def test_projection_loader_pins_routes_and_migration_in_one_read(self):
        """Given a v2 envelope, When loaded, Then both pieces share one exchange read."""
        packet = _packet()
        migration = {"version": 1, "basisRecordIds": [packet["id"]], "pages": [{
            "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md",
            "topicId": "d" * 64, "title": "投放决策", "topic": "投放",
            "decisionObject": "样例首轮素材", "body": "# 投放", "previousPages": []}]}
        envelope = {"version": 2, "baselineId": BASELINE, "reviewedAt": "2026-09-17T00:00:00Z",
                    "groups": [], "migration": migration}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "v2/topic-routes.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            config = {"exchange": {"root": temporary}}
            original_read = __import__("shared_files").SharedFiles.read
            with patch("topic_routes.SharedFiles.read", autospec=True,
                       side_effect=lambda instance, relative, **kwargs: original_read(instance, relative, **kwargs)) as read:
                projection = load_topic_projection(config, BASELINE, [packet])
            self.assertEqual(projection["topicMigration"], migration)
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
