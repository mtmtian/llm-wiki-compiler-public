"""Given/When/Then acceptance tests for the pure-iCloud replica protocol."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import digest, load_json
from replica import initialize_baseline, publish_record, read_baseline, read_records, replica_status, sync_replica
from replica_records import canonical, validate_packet


class ReplicaTests(unittest.TestCase):
    """Exercise two independent machine states against one shared exchange."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.shared, self.exchange = root / "shared", root / "exchange"
        (self.shared / "sources").mkdir(parents=True)
        (self.shared / "wiki/concepts").mkdir(parents=True)
        (self.shared / ".llmwiki").mkdir()
        (self.shared / "sources/guide.md").write_text("baseline guide", encoding="utf-8")
        (self.shared / "sources/notes.txt").write_text("baseline notes", encoding="utf-8")
        (self.shared / "wiki/concepts/base.md").write_text("# Base", encoding="utf-8")
        (self.shared / ".llmwiki/config.json").write_text('{"version":1}', encoding="utf-8")
        (self.shared / "candidates/nope.md").parent.mkdir()
        (self.shared / "candidates/nope.md").write_text("private candidate", encoding="utf-8")
        self.configs = {machine: self._config(root, machine) for machine in ("a", "b")}
        initialize_baseline(self.configs["a"])

    def tearDown(self):
        self.temp.cleanup()

    def _config(self, root, machine):
        state = root / machine
        return {"machineId": machine, "publishEnabled": True, "model": "gpt-test", "stateDir": str(state),
                "wikiRoot": str(state / "replica/current"), "sharedWikiRoot": str(self.shared),
                "projects": {"project": {"label": "Project", "pages": []},
                             "other": {"label": "Other", "pages": []}},
                "exchange": {"protocolVersion": 2, "root": str(self.exchange), "participants": ["a", "b"]}}

    def _submission(self, machine, text, use_when, source, project="project"):
        evidence = {"id": source, "kind": "user", "text": text, "sha256": digest(text),
                    "originalSha256": digest("full:" + text), "observedAt": "2026-09-15T00:00:00Z", "locator": source}
        claim = {"text": text, "quote": text, "evidenceId": source, "useWhen": use_when, "title": source,
                 "topic": "replica", "slug": source, "targetPageId": None, "kind": "decision",
                 "status": "decided", "rationale": "reviewed quote"}
        label = project.title()
        job = {"id": "job-" + source, "projectId": project, "projectLabel": label,
               "createdAt": "2026-09-15T00:00:00Z", "basisRecordIds": []}
        result = {"status": "submitted", "publishedPageIds": [], "contribution": {"claims": [claim], "evidence": [evidence]}}
        return job, result

    def test_baseline_is_immutable_and_excludes_operational_folders(self):
        """Given a shared Wiki, When baseline initializes, Then only approved files are captured."""
        before = (self.shared / "wiki/concepts/base.md").read_bytes()
        manifest = read_baseline(self.configs["b"])
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"sources/guide.md", "sources/notes.txt", "wiki/concepts/base.md", ".llmwiki/config.json"})
        self.assertEqual((self.shared / "wiki/concepts/base.md").read_bytes(), before)
        self.assertFalse((self.shared / "candidates/nope.md").read_bytes() == b"")

    def test_baseline_size_is_checked_before_immutable_write(self):
        """The exact serialized limit is accepted; one extra byte never poisons shared state."""
        encoded = (canonical(read_baseline(self.configs["a"])) + "\n").encode("utf-8")
        fresh = copy.deepcopy(self.configs["a"])
        fresh["exchange"]["root"] = str(Path(self.temp.name) / "fresh-exchange")
        destination = Path(fresh["exchange"]["root"]) / "v2/baseline.json"
        with patch("replica.MAX_BASELINE_BYTES", len(encoded) - 1):
            with self.assertRaisesRegex(ValueError, "baseline exceeds"):
                initialize_baseline(fresh)
        self.assertFalse(destination.exists())
        with patch("replica.MAX_BASELINE_BYTES", len(encoded)):
            initialize_baseline(fresh)
        self.assertEqual(destination.read_bytes(), encoded)

    def test_all_declared_machines_publish_and_retry_is_idempotent(self):
        """Given two publishing machines, When both retry, Then both packets are shared exactly once."""
        first = []
        for machine in ("a", "b"):
            job, result = self._submission(machine, "Use stable IDs", "always", machine + "-one")
            first.append(publish_record(self.configs[machine], job, result))
        job, result = self._submission("a", "Use stable IDs", "always", "a-one")
        retry = publish_record(self.configs["a"], job, result)
        self.assertEqual(retry["publicationId"], first[0]["publicationId"])
        self.assertEqual(len(read_records(self.configs["b"])), 2)

    def test_assistant_lesson_publishes_and_peer_validates_exact_quote(self):
        """Dated analysis survives independent publication without becoming a rule."""
        job, result = self._submission("a", "Separate acquisition cost from retention", "Analyzing growth", "analysis")
        result["contribution"]["evidence"][0]["kind"] = "assistant"
        result["contribution"]["claims"][0].update(kind="lesson", status="historical")
        published = publish_record(self.configs["a"], job, result)
        records = read_records(self.configs["b"])
        self.assertEqual(records[0]["id"], published["publicationId"])
        self.assertEqual(records[0]["payload"]["claims"][0]["quote"], result["contribution"]["evidence"][0]["text"])
        baseline = read_baseline(self.configs["a"])["snapshotId"]
        for kind, status in (("decision", "decided"), ("fact", "historical"), ("constraint", "historical"), ("lesson", "uncertain"), ("lesson", "decided")):
            with self.subTest(kind=kind, status=status):
                bad = copy.deepcopy(records[0])
                bad["payload"]["claims"][0].update(kind=kind, status=status)
                bad["id"] = digest(canonical(bad["payload"]))
                with self.assertRaises(ValueError):
                    validate_packet(bad, "a", baseline)
                result["contribution"]["claims"][0].update(kind=kind, status=status)
                with self.assertRaises(ValueError):
                    publish_record(self.configs["a"], job, result)

    def test_different_use_when_and_sources_are_retained(self):
        """Given equal text, When meaning and source differ, Then both records remain."""
        for suffix, use_when in (("first", "when building"), ("second", "when reviewing")):
            job, result = self._submission("a", "Keep the source", use_when, "a-" + suffix)
            publish_record(self.configs["a"], job, result)
        records = read_records(self.configs["b"])
        self.assertEqual(len(records), 2)
        self.assertEqual({item["payload"]["claims"][0]["useWhen"] for item in records}, {"when building", "when reviewing"})

    def test_invalid_files_are_reported_and_verified_cache_recovers(self):
        """Given a cached packet, When its cloud bytes are corrupted, Then cache stays viewable and errors are visible."""
        job, result = self._submission("a", "Cache me", "always", "cache")
        sent = publish_record(self.configs["a"], job, result)
        self.assertEqual(len(read_records(self.configs["b"])), 1)
        packet_path = self.exchange / "v2/publications/a" / (sent["publicationId"] + ".json")
        packet_path.write_text("{", encoding="utf-8")
        (self.exchange / "v2/publications/unknown").mkdir(parents=True)
        (self.exchange / "v2/publications/unknown/bad.json").write_text("{}", encoding="utf-8")
        (self.exchange / "v2/publications/a/half.json").write_text("{", encoding="utf-8")
        self.assertEqual(len(read_records(self.configs["b"])), 1)
        status = replica_status(self.configs["b"])
        self.assertGreaterEqual(len(status["errors"]), 2)

    def test_cached_baseline_is_pinned_when_shared_snapshot_changes(self):
        """Given a cached baseline, When shared bytes change, Then the local snapshot stays pinned."""
        original = read_baseline(self.configs["a"])
        read_records(self.configs["a"])
        changed = {"version": 2, "files": [{"path": "sources/guide.md", "text": "changed", "sha256": digest("changed")} ]}
        changed["snapshotId"] = digest(canonical(changed["files"]))
        baseline_path = self.exchange / "v2/baseline.json"
        baseline_path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual(read_records(self.configs["a"]), [])
        self.assertEqual(load_json(self.configs["a"]["stateDir"] + "/replica-baseline.json")["snapshotId"], original["snapshotId"])
        self.assertTrue(replica_status(self.configs["a"])["errors"])

    def test_malformed_claim_packet_is_skipped(self):
        """Given a packet with a valid envelope hash but malformed claim fields, Then it is not imported."""
        job, result = self._submission("a", "Valid", "always", "valid")
        valid = publish_record(self.configs["a"], job, result)
        packet_path = self.exchange / "v2/publications/a" / (valid["publicationId"] + ".json")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["payload"]["claims"][0].pop("topic")
        packet["id"] = digest(canonical(packet["payload"]))
        bad_path = packet_path.with_name(packet["id"] + ".json")
        bad_path.write_text(json.dumps(packet), encoding="utf-8")
        self.assertEqual(len(read_records(self.configs["b"])), 1)
        self.assertTrue(replica_status(self.configs["b"])["errors"])

    def test_slug_without_unicode_letter_or_number_is_skipped(self):
        """Given a valid-hash packet with an unusable slug, Then healthy records still sync."""
        job, result = self._submission("a", "Healthy", "always", "healthy")
        sent = publish_record(self.configs["a"], job, result)
        packet_path = self.exchange / "v2/publications/a" / (sent["publicationId"] + ".json")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["payload"]["claims"][0]["slug"] = "!!!"
        packet["id"] = digest(canonical(packet["payload"]))
        packet_path = packet_path.with_name(packet["id"] + ".json")
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        self.assertEqual(len(read_records(self.configs["b"])), 1)
        self.assertTrue(replica_status(self.configs["b"])["errors"])

    def test_publication_parent_symlink_is_rejected(self):
        """Given a redirected machine directory, When publishing, Then no file escapes the exchange."""
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        target = self.exchange / "v2/publications/b"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside, target_is_directory=True)
        job, result = self._submission("b", "No escape", "always", "escape")
        with self.assertRaises(ValueError):
            publish_record(self.configs["b"], job, result)
        self.assertEqual(list(outside.iterdir()), [])

    def test_sync_is_order_independent_and_failure_keeps_current(self):
        """Given records in arbitrary order, When sync runs or fails, Then digest is stable and old view survives."""
        for suffix in ("one", "two"):
            job, result = self._submission("a", suffix, "always", suffix)
            publish_record(self.configs["a"], job, result)
        seen = []

        def materialize(stage, records):
            seen.append((stage, [item["id"] for item in records]))
            Path(stage, "materialized.txt").write_text("ok", encoding="utf-8")
            return {"pages": 2, "conflicts": []}

        first = sync_replica(self.configs["a"], materialize)
        second = sync_replica(self.configs["b"], materialize)
        self.assertEqual(first["digest"], second["digest"])
        old_current = (Path(self.configs["a"]["stateDir"]) / "replica/current").resolve()
        job, result = self._submission("a", "new", "always", "new")
        publish_record(self.configs["a"], job, result)
        with self.assertRaises(RuntimeError):
            sync_replica(self.configs["a"], lambda _stage, _records: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual((Path(self.configs["a"]["stateDir"]) / "replica/current").resolve(), old_current)

    def test_conflict_records_are_hidden_and_history_reuse_keeps_conflicts(self):
        """Given a conflict response, When a generation is reused, Then visibility and conflicts persist."""
        for suffix in ("left", "right"):
            job, result = self._submission("a", suffix, "always", suffix)
            publish_record(self.configs["a"], job, result)
        packets = read_records(self.configs["a"])
        blocked = packets[0]["id"]
        conflict = lambda _stage, _records: {"pages": 1, "conflicts": [{"recordIds": [blocked]}]}
        first = sync_replica(self.configs["a"], conflict)
        self.assertEqual(first["fullyVisibleRecordIds"], [packets[1]["id"]])
        marker = Path(self.temp.name) / "worker-version"
        marker.write_text("v2", encoding="utf-8")
        changed = dict(self.configs["a"], worker=str(marker))
        sync_replica(changed, lambda _stage, _records: {"pages": 1, "conflicts": []})
        reused = sync_replica(self.configs["a"], conflict)
        self.assertEqual(reused["conflicts"][0]["recordIds"], [blocked])
        self.assertEqual(reused["fullyVisibleRecordIds"], [packets[1]["id"]])

    def test_status_recovery_recomputes_response_after_current_switch(self):
        """Given current switches before status persistence, Then recovery trusts generation response."""
        sync_replica(self.configs["a"], lambda _stage, _records: {"pages": 0, "conflicts": []})
        for suffix in ("left", "right"):
            job, result = self._submission("a", suffix, "always", "crash-" + suffix)
            publish_record(self.configs["a"], job, result)
        packets = read_records(self.configs["a"])
        blocked = packets[0]["id"]
        response = {"pages": 1, "conflicts": [{"recordIds": [blocked]}]}
        with patch("replica_generation._write_status", side_effect=RuntimeError("status crash")):
            with self.assertRaises(RuntimeError):
                sync_replica(self.configs["a"], lambda _stage, _records: response)
        recovered = sync_replica(self.configs["a"], lambda _stage, _records: response)
        self.assertEqual(recovered["conflicts"], response["conflicts"])
        self.assertEqual(recovered["fullyVisibleRecordIds"], [packets[1]["id"]])

    def test_cross_project_large_record_set_does_not_overflow_target_basis(self):
        """Given 1025 records for another project, Then target basis stays scoped and bounded."""
        worker = Path(self.temp.name) / "large-worker.py"
        worker.write_text("import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({'pages': len(v['records']), 'conflicts': []}))\n", encoding="utf-8")
        config = copy.deepcopy(self.configs["a"])
        config.update(node=sys.executable, worker=str(worker))
        for index in range(1025):
            job, result = self._submission("a", "other-" + str(index), "always", "other-" + str(index), project="other")
            publish_record(config, job, result)
        target_job, target_result = self._submission("a", "target", "always", "target", project="project")
        publish_record(config, target_job, target_result)
        target_job.pop("basisRecordIds", None)
        queue_path = Path(config["stateDir"]) / "queue" / "target.json"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(json.dumps(target_job), encoding="utf-8")
        from queue_replica import prepare
        prepared = prepare(config, {"job": target_job}, queue_path)
        self.assertEqual(prepared["wikiRoot"], str(Path(config["wikiRoot"]).resolve()))
        self.assertEqual(len(target_job["basisRecordIds"]), 1)
        self.assertLessEqual(len(target_job["basisRecordIds"]), 1024)

    def test_missing_generation_metadata_fails_closed(self):
        """Given an active generation, When response metadata disappears, Then it is quarantined before reuse."""
        first = sync_replica(self.configs["a"], lambda _stage, _records: {"pages": 0, "conflicts": []})
        current = Path(first["generationRoot"])
        (current / ".llmwiki/replica-response.json").unlink()
        with self.assertRaises(ValueError):
            sync_replica(self.configs["a"], lambda _stage, _records: {"pages": 0, "conflicts": []})
        state = Path(self.configs['a']['stateDir'])
        self.assertFalse((state / 'replica/current').exists())
        self.assertTrue(replica_status(self.configs['a'])['errors'])
        repaired = sync_replica(self.configs['a'], lambda _stage, _records: {'pages': 0, 'conflicts': []})
        self.assertTrue(Path(repaired['generationRoot'], '.llmwiki/replica-response.json').is_file())

    def test_review_keeps_generation_when_another_sync_switches_current(self):
        """Given a pinned review, When another sync wins, Then its root and basis stay paired."""
        import queue_replica
        from concurrent.futures import ThreadPoolExecutor
        from common import save_json
        config = self.configs["a"]
        first = sync_replica(config, lambda _stage, _records: {"pages": 0, "conflicts": []})
        job = {"id": "review", "projectId": "project"}
        queued = Path(config["stateDir"]) / "queue/review.json"

        pending = []

        def persist_then_sync(path, value):
            save_json(path, value)
            other_job, result = self._submission("b", "Concurrent", "always", "concurrent")
            publish_record(self.configs["b"], other_job, result)
            pending.append(pool.submit(sync_replica, config, lambda _stage, _records: {"pages": 1, "conflicts": []}))

        with ThreadPoolExecutor(max_workers=1) as pool:
            with patch.object(queue_replica, "save_json", side_effect=persist_then_sync):
                active = queue_replica.prepare(config, {"job": job}, queued)
            for completed in pending:
                completed.result(timeout=5)
        self.assertNotEqual(str(Path(config["wikiRoot"]).resolve()), first["generationRoot"])
        self.assertEqual(active["wikiRoot"], first["generationRoot"])
        self.assertEqual(job["basisRecordIds"], [])

    def test_default_materializer_worker_contract(self):
        """Given a worker command, When sync invokes materialize, Then staged config and records are JSON."""
        worker = Path(self.temp.name) / "worker.py"
        worker.write_text("import json,sys\nv=json.load(sys.stdin)\nassert v['config']['wikiRoot']\nassert v['records'][0]['payload']['claims'][0]['quote'] == '中文证据'\nprint(json.dumps({'pages': len(v['records']), 'conflicts': []}))\n", encoding="utf-8")
        config = copy.deepcopy(self.configs["a"])
        config.update(node=sys.executable, worker=str(worker))
        job, result = self._submission("a", "中文证据", "always", "utf8")
        publish_record(config, job, result)
        result = sync_replica(config)
        self.assertEqual(result["count"], 1)
        self.assertTrue(Path(result["generationRoot"]).is_dir())

    def test_materializer_worker_preserves_utf8_evidence(self):
        """Given a UTF-8 publication, When the worker parses JSON, Then evidence remains intact."""
        worker = Path(self.temp.name) / "worker.py"
        worker.write_text("import json,sys\nv=json.load(sys.stdin)\nassert v['records'][0]['payload']['claims'][0]['quote'] == '中文证据'\nprint(json.dumps({'pages': 1, 'conflicts': []}))\n", encoding="utf-8")
        config = copy.deepcopy(self.configs["a"])
        config.update(node=sys.executable, worker=str(worker))
        job, result = self._submission("a", "中文证据", "always", "utf8-split")
        publish_record(config, job, result)
        result = sync_replica(config)
        self.assertEqual(result["count"], 1)

    def test_sync_promotes_new_pages_to_shared_obsidian_without_touching_baseline(self):
        """Given an accepted publication, When a replica syncs, Then new pages are shared."""
        self.configs["a"]["exchange"]["materializerMachineId"] = "a"
        job, result = self._submission("a", "Shared decision", "always", "shared")
        publish_record(self.configs["a"], job, result)

        def materialize(stage, _records):
            page = Path(stage) / "wiki/concepts/record-shared.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("# Shared decision\n", encoding="utf-8")
            source = Path(stage) / "sources/knowledge-flow-shared.md"
            source.write_text("evidence\n", encoding="utf-8")
            return {"pages": 1, "conflicts": []}

        before = (self.shared / "wiki/concepts/base.md").read_bytes()
        sync_replica(self.configs["a"], materialize)
        self.assertEqual((self.shared / "wiki/concepts/base.md").read_bytes(), before)
        self.assertEqual((self.shared / "wiki/concepts/record-shared.md").read_text(encoding="utf-8"), "# Shared decision\n")
        self.assertEqual((self.shared / "sources/knowledge-flow-shared.md").read_text(encoding="utf-8"), "evidence\n")


if __name__ == "__main__":
    unittest.main()
