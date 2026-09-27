"""Exercise out-of-order receipt and packet arrival through normal replica sync.

The publication store, cache, generation switch and receipt readers are real.
A small materializer adapter returns the existing legacy routing diagnostic;
renderer byte compatibility is covered by the TypeScript replay tests.
"""

import json
import unittest
from pathlib import Path

from replica import publish_record, read_baseline, read_records, sync_replica
import test_replica


class PublicationResolutionSyncTests(unittest.TestCase):
    """Independent iCloud files must not become an all-or-nothing intake gate."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.writer = self.fixture.configs["a"]
        self.reader = self.fixture.configs["b"]
        self.old_id = self._publish("old", revision=False)
        self.new_id = self._publish("replacement", revision=True)
        self.replacement = next(record for record in read_records(self.writer) if record["id"] == self.new_id)
        receipt = {"version": 1, "baselineId": read_baseline(self.writer)["snapshotId"],
                   "reviewedAt": "2026-09-26T00:00:00Z", "resolutions": [{
                       "recordId": self.old_id, "coveredBy": self.new_id,
                       "claimMappings": [{"claimIndex": 0, "coveredByIndexes": [0]}],
                       "reason": "Both claims and their original evidence were reviewed."}]}
        (self.fixture.exchange / "v2/publication-resolutions.json").write_text(json.dumps(receipt))

    def _publish(self, source, revision):
        """Publish a valid packet before simulating delayed delivery to a peer."""
        job, result = self.fixture._submission("a", "Keep source evidence", "always", source)
        contribution = result["contribution"]
        claim = contribution["claims"][0]
        claim["targetPageId"] = "concepts/recovery"
        if revision:
            claim["decisionObject"] = "Source retention"
            contribution["topicRevisions"] = [{
                "pageId": claim["targetPageId"], "topicId": "d" * 64, "title": source,
                "topic": claim["topic"], "decisionObject": claim["decisionObject"],
                "basisHash": None, "body": "{{claim:0}}", "claimIndexes": [0]}]
        return publish_record(self.writer, job, result)["publicationId"]

    def _materialize(self, stage, records):
        """Render one accepted source and preserve the independent legacy warning."""
        identifiers = {record["id"] for record in records}
        if self.new_id in identifiers:
            claim = self.replacement["payload"]["claims"][0]
            source_name = f"Project-2026-09-15-{self.new_id[:12]}.md"
            source = f"# Project\n\n## 1. {claim['title']}\n\n{claim['quote']}\n"
            page = (f"---\nprojectId: project\nsources:\n  - {source_name}\n"
                    f'knowledgePublicationRefs:\n  - "{self.new_id}:0"\n---\n\n'
                    f"Current decision ^[{source_name}:5]\n")
            Path(stage, "sources", source_name).write_text(source)
            Path(stage, "wiki/concepts/recovery.md").write_text(page)
        conflicts = [{"recordIds": [self.old_id], "reason": "legacy routing conflict"}]
        return {"pages": 1, "conflicts": conflicts if self.old_id in identifiers else []}

    def _arrival_order(self, missing_id):
        """Keep a delayed receipt inactive, then activate it after its packet arrives."""
        packet = self.fixture.exchange / "v2/publications/a" / (missing_id + ".json")
        delayed = Path(self.fixture.temp.name) / "delayed.json"
        packet.rename(delayed)
        first = sync_replica(self.reader, self._materialize)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["errors"], [])
        self.assertNotIn("resolvedConflicts", first)
        self.assertEqual(len(first["conflicts"]), 1 if missing_id == self.new_id else 0)
        self.assertEqual(Path(self.reader["wikiRoot"]).resolve(), Path(first["generationRoot"]))
        delayed.rename(packet)
        second = sync_replica(self.reader, self._materialize)
        self.assertEqual(second["count"], 2)
        self.assertEqual(second["conflicts"], [])
        self.assertEqual(second["resolvedConflicts"][0]["recordId"], self.old_id)
        self.assertEqual(second["fullyVisibleRecordIds"], [self.new_id])
        repeated = sync_replica(self.reader, self._materialize)
        self.assertEqual(repeated["digest"], second["digest"])
        self.assertEqual(repeated["resolvedConflicts"], second["resolvedConflicts"])

    def test_receipt_before_replacement_preserves_conflict_and_sync_until_packet_arrives(self):
        """Given a delayed replacement, When receipt arrives first, Then normal intake continues."""
        self._arrival_order(self.new_id)

    def test_receipt_before_legacy_record_preserves_sync_until_packet_arrives(self):
        """Given a delayed legacy packet, When receipt arrives first, Then no evidence is invented."""
        self._arrival_order(self.old_id)


if __name__ == "__main__":
    unittest.main()
