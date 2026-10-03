"""Given/When/Then tests for version 3 topic-route manifests carrying reviewed topic merges.

A merge names a surviving page among at least two revision-layer pages, the records it absorbed and
the reviewed body. The manifest is validated strictly before a generation can switch; old runtimes
reject version 3, so a merge can never be half understood. Pages the legacy migration owns stay out.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

import test_replica
from replica import publish_record, read_baseline, sync_replica
from replica_generation import _routing_hash
from replica_records import canonical
from shared_retirement import desired_retirement
from topic_routes import _decode_manifest

BASELINE = "b" * 64
ALPHA, BETA, GAMMA = "concepts/alpha", "concepts/beta", "concepts/gamma"


def record(char: str, *pages: str) -> dict:
    """A known record whose whole-page revisions touch `pages`."""
    return {"id": char * 64, "payload": {"topicRevisions": [{"pageId": page} for page in pages]}}


RECORDS = [record("1", ALPHA), record("2", BETA), record("3", GAMMA)]


def merge(**overrides) -> dict:
    value = {"pageId": ALPHA, "title": "甲乙合并", "topic": "样例主题", "decisionObject": "样例对象",
             "body": "## 结论\n\n合并后的正文", "previousPages": [{"pageId": BETA, "sha256": "e" * 64},
                                                        {"pageId": ALPHA, "sha256": "d" * 64}],
             "absorbedRecordIds": ["2" * 64, "1" * 64], "mergedAt": "2026-10-04T00:00:00Z", "reason": "同属一个样例对象"}
    value.update(overrides)
    return value


def manifest(merges, **extra) -> dict:
    value = {"version": 3, "baselineId": BASELINE, "reviewedAt": "2026-10-04T00:00:00Z", "groups": [], "merges": merges}
    value.update(extra)
    return value


def decode(value: dict) -> dict:
    return _decode_manifest(canonical(value).encode("utf-8"), BASELINE, RECORDS)


class TopicMergeManifestTests(unittest.TestCase):
    """Merges must be strict, canonical inputs that never overlap the legacy migration."""

    def test_valid_merge_is_normalized(self):
        """Given a version 3 manifest, When decoded, Then its merge is canonical and no migration is implied."""
        decoded = decode(manifest([merge()]))
        self.assertIsNone(decoded["topicMigration"])
        [value] = decoded["topicMerges"]
        self.assertEqual([page["pageId"] for page in value["previousPages"]], [ALPHA, BETA])
        self.assertEqual(value["absorbedRecordIds"], ["1" * 64, "2" * 64])

    def test_invalid_merges_fail_closed(self):
        """Given a malformed merge or manifest shape, When decoded, Then validation fails."""
        cases = {
            "target outside previous pages": manifest([merge(pageId=GAMMA)]),
            "single previous page": manifest([merge(previousPages=[{"pageId": ALPHA, "sha256": "d" * 64}])]),
            "unknown absorbed record": manifest([merge(absorbedRecordIds=["9" * 64])]),
            "absorbed record revising none": manifest([merge(absorbedRecordIds=["3" * 64])]),
            "mergedAt without timezone": manifest([merge(mergedAt="2026-10-04T00:00:00")]),
            "unexpected field": manifest([merge(extra=True)]),
            "empty merges": manifest([]),
            "page merged twice": manifest([merge(), merge(pageId=GAMMA, previousPages=[
                {"pageId": GAMMA, "sha256": "c" * 64}, {"pageId": BETA, "sha256": "e" * 64}],
                absorbedRecordIds=["3" * 64])]),
            "version 2 with merges": manifest([merge()], version=2),
            "version 3 without merges": {key: value for key, value in manifest([merge()]).items() if key != "merges"},
        }
        for name, value in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                decode(value)

    def test_legacy_migration_pages_cannot_be_merged(self):
        """Given a page the legacy migration owns, When a merge includes it, Then the manifest is rejected."""
        migration = {"version": 1, "basisRecordIds": ["1" * 64], "pages": [{
            "projectId": "project", "projectLabel": "Project", "pageId": "concepts/legacy", "topicId": "f" * 64,
            "title": "旧页", "topic": "旧主题", "decisionObject": "旧对象", "body": "旧正文",
            "previousPages": [{"pageId": BETA, "sha256": "e" * 64}]}]}
        with self.assertRaisesRegex(ValueError, "topic merge pages are invalid"):
            decode(manifest([merge()], migration=migration))

    def test_routing_identity_changes_only_with_merges(self):
        """Given no merges, Then the generation identity is unchanged; a merge changes it."""
        config = {"projects": {}}
        self.assertEqual(_routing_hash(config, [], None), _routing_hash(config, [], None, None))
        self.assertNotEqual(_routing_hash(config, [], None), _routing_hash(config, [], None, [merge()]))

    def test_merged_baseline_page_receives_a_tombstone(self):
        """Given a merge that removes a baseline page, Then the shared vault retires it instead of restoring it."""
        baseline = {f"wiki/{ALPHA}.md": b"alpha", f"wiki/{BETA}.md": b"beta"}
        retired = desired_retirement({"topicMerges": [merge()]}, baseline, {f"wiki/{ALPHA}.md": b"merged"})
        self.assertEqual(retired, {f"wiki/{BETA}.md"})


class TopicMergeSyncTests(unittest.TestCase):
    """A valid merge reaches the materializer and builds a new generation."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = copy.deepcopy(self.fixture.configs["b"])
        worker = Path(self.fixture.temp.name) / "worker.py"
        worker.write_text(
            "import json, pathlib, sys\n"
            "value = json.load(sys.stdin)\n"
            "root = pathlib.Path(value['config']['wikiRoot']) / 'wiki/concepts'\n"
            "root.mkdir(parents=True, exist_ok=True)\n"
            "(root / 'merges.md').write_text(json.dumps(value['config'].get('topicMerges'), ensure_ascii=False))\n"
            "print(json.dumps({'pages': 1, 'conflicts': []}))\n", encoding="utf-8")
        self.config.update(node=sys.executable, worker=str(worker))

    def publish_revision(self, source: str, page_id: str) -> str:
        """Publish one accepted record whose whole-page revision creates `page_id`."""
        job, result = self.fixture._submission("a", f"{source} evidence", "always", source)
        contribution = result["contribution"]
        claim = contribution["claims"][0]
        claim["targetPageId"], claim["decisionObject"] = page_id, f"{source} object"
        contribution["topicRevisions"] = [{"pageId": page_id, "topicId": "d" * 64, "title": source,
                                           "topic": claim["topic"], "decisionObject": claim["decisionObject"],
                                           "basisHash": None, "body": "{{claim:0}}", "claimIndexes": [0]}]
        return publish_record(self.fixture.configs["a"], job, result)["publicationId"]

    def test_merge_reaches_the_materializer_in_a_new_generation(self):
        """Given two revision pages and a reviewed merge, When sync runs, Then the worker receives the merge."""
        absorbed = [self.publish_revision("alpha", ALPHA), self.publish_revision("beta", BETA)]
        before = sync_replica(self.config)["digest"]
        value = manifest([merge(absorbedRecordIds=absorbed)], baselineId=read_baseline(self.config)["snapshotId"])
        target = Path(self.config["exchange"]["root"]) / "v2/topic-routes.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical(value) + "\n", encoding="utf-8")
        status = sync_replica(self.config)
        rendered = Path(status["generationRoot"], "wiki/concepts/merges.md").read_text(encoding="utf-8")
        self.assertNotEqual(status["digest"], before)
        self.assertEqual([item["pageId"] for item in json.loads(rendered)], [ALPHA])


if __name__ == "__main__":
    unittest.main()
