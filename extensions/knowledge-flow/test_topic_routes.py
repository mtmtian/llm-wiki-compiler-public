"""Given/When/Then tests for reviewed legacy topic route manifests."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import test_replica
from replica import read_baseline, read_records, replica_status, sync_replica
from replica_records import canonical
from topic_routes import validate_topic_routes


class TopicRouteTests(unittest.TestCase):
    """Route manifests must be strict, immutable inputs to one sync."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = copy.deepcopy(self.fixture.configs["b"])
        self.exchange = Path(self.config["exchange"]["root"])
        worker = Path(self.fixture.temp.name) / "worker.py"
        worker.write_text(
            "import json, pathlib, sys\n"
            "value = json.load(sys.stdin)\n"
            "root = pathlib.Path(value['config']['wikiRoot']) / 'wiki/concepts'\n"
            "root.mkdir(parents=True, exist_ok=True)\n"
            "(root / 'route.md').write_text(json.dumps(value['config'].get('topicRoutes', []), ensure_ascii=False))\n"
            "print(json.dumps({'pages': 1, 'conflicts': []}))\n",
            encoding="utf-8",
        )
        self.config.update(node=str(Path(__import__("sys").executable)), worker=str(worker))

    def publish_legacy(self, name: str, project: str = "project") -> str:
        """Publish one old claim that has no decisionObject metadata."""
        job, result = self.fixture._submission("a", name, "always", name, project=project)
        from replica import publish_record
        sent = publish_record(self.fixture.configs["a"], job, result)
        return str(sent["publicationId"])

    def write_routes(self, groups, baseline_id: str | None = None) -> None:
        """Write one reviewed route manifest directly into the disposable exchange."""
        value = {"version": 1, "baselineId": baseline_id or read_baseline(self.config)["snapshotId"],
                 "reviewedAt": "2026-09-17T00:00:00Z", "groups": groups}
        target = self.exchange / "v2/topic-routes.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical(value) + "\n", encoding="utf-8")

    def group(self, record_id: str, project: str = "project", topic: str = "投放", obj: str = "示例素材渠道"):
        """Build one manifest group for a publication's first legacy claim."""
        return {"projectId": project, "topic": topic, "decisionObject": obj,
                "claimRefs": [f"{record_id}:0"]}

    def test_reviewed_routes_reach_worker_and_do_not_persist_in_caller_config(self):
        """Given a valid route, When sync runs, Then the worker receives normalized routes."""
        record_id = self.publish_legacy("legacy")
        self.write_routes([self.group(record_id)])
        status = sync_replica(self.config)
        rendered = Path(status["generationRoot"], "wiki/concepts/route.md").read_text(encoding="utf-8")
        self.assertEqual(json.loads(rendered)[0]["claimRefs"], [f"{record_id}:0"])
        self.assertNotIn("topicRoutes", self.config)

    def test_route_group_and_ref_order_have_one_generation_identity(self):
        """Given equivalent groups in another order, When sync retries, Then the generation is reused."""
        first = self.publish_legacy("first")
        second = self.publish_legacy("second")
        self.write_routes([self.group(second, topic="B"), self.group(first, topic="A")])
        initial = sync_replica(self.config)
        self.write_routes([self.group(first, topic="A"), self.group(second, topic="B")])
        replay = sync_replica(self.config)
        self.assertEqual(initial["digest"], replay["digest"])
        first_rendered = Path(initial["generationRoot"], "wiki/concepts/route.md").read_text(encoding="utf-8")
        replay_rendered = Path(replay["generationRoot"], "wiki/concepts/route.md").read_text(encoding="utf-8")
        self.assertEqual(first_rendered, replay_rendered)

    def test_changed_route_manifest_builds_a_new_generation(self):
        """Given one reviewed destination, When its object changes, Then a new view is built."""
        record_id = self.publish_legacy("changed")
        self.write_routes([self.group(record_id, obj="Google Ads")])
        initial = sync_replica(self.config)
        self.write_routes([self.group(record_id, obj="TikTok")])
        changed = sync_replica(self.config)
        self.assertNotEqual(initial["digest"], changed["digest"])
        rendered = Path(changed["generationRoot"], "wiki/concepts/route.md").read_text(encoding="utf-8")
        self.assertIn("TikTok", rendered)

    def test_invalid_or_unknown_route_keeps_current_and_records_failure(self):
        """Given a healthy current, When a route is invalid, Then current remains unchanged."""
        record_id = self.publish_legacy("healthy")
        self.write_routes([self.group(record_id)])
        initial = sync_replica(self.config)
        for invalid in (
            {"version": 1, "baselineId": "f" * 64, "reviewedAt": "2026-09-17T00:00:00Z", "groups": []},
            {"version": 1, "baselineId": read_baseline(self.config)["snapshotId"],
             "reviewedAt": "2026-09-17T00:00:00Z", "groups": [self.group("a" * 64)]},
        ):
            (self.exchange / "v2/topic-routes.json").write_text(canonical(invalid) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                sync_replica(self.config)
            self.assertEqual(Path(self.config["wikiRoot"]).resolve(), Path(initial["generationRoot"]).resolve())
            self.assertEqual(replica_status(self.config)["lastError"], "ValueError")

    def test_validation_rejects_foreign_duplicate_index_nonlegacy_and_bool_version(self):
        """Given route references, When identity or type checks fail, Then validation rejects them."""
        record_id = self.publish_legacy("strict")
        baseline = read_baseline(self.config)["snapshotId"]
        records = read_records(self.config)
        cases = (
            {"version": 1, "baselineId": baseline, "reviewedAt": "2026-09-17T00:00:00Z",
             "groups": [self.group(record_id, project="other")]},
            {"version": 1, "baselineId": baseline, "reviewedAt": "2026-09-17T00:00:00Z",
             "groups": [self.group(record_id), self.group(record_id, topic="重复")]},
            {"version": 1, "baselineId": baseline, "reviewedAt": "2026-09-17T00:00:00Z",
             "groups": [{"projectId": "project", "topic": "投放", "decisionObject": "示例素材渠道",
                          "claimRefs": [f"{record_id}:1"]}]},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_topic_routes(value, baseline, records)
        legacy = copy.deepcopy(records)
        legacy[0]["payload"]["claims"][0]["decisionObject"] = "已存在"
        valid = {"version": 1, "baselineId": baseline, "reviewedAt": "2026-09-17T00:00:00Z",
                 "groups": [self.group(record_id)]}
        with self.assertRaises(ValueError):
            validate_topic_routes(valid, baseline, legacy)
        invalid_version = dict(valid, version=True)
        with self.assertRaises(ValueError):
            validate_topic_routes(invalid_version, baseline, records)

    def test_missing_manifest_is_an_empty_legacy_route_set(self):
        """Given no optional route file, When sync runs, Then old claims still build the private view."""
        self.publish_legacy("without-routes")
        status = sync_replica(self.config)
        self.assertEqual(json.loads(Path(status["generationRoot"], "wiki/concepts/route.md").read_text()), [])

    def test_symlink_manifest_is_rejected_before_switch(self):
        """Given a redirected optional file, When sync runs, Then no generation is exposed."""
        self.publish_legacy("symlink")
        outside = Path(self.fixture.temp.name) / "outside-routes.json"
        outside.write_text("{}", encoding="utf-8")
        target = self.exchange / "v2/topic-routes.json"
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            sync_replica(self.config)
        self.assertFalse((Path(self.config["stateDir"]) / "replica/current").exists())


if __name__ == "__main__":
    unittest.main()
