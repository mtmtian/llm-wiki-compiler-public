"""Exercise the shared semantic-topic activation gate with real temporary files."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from common import config_from, page_ids, save_json
from semantic_scope import CAPABILITY, activate, readiness, require_ready, require_publication
from hooks import prepare_session_job
from queue_batch import batch_key as _batch_key, merge_batch as _merge_batch


class SemanticScopeTests(unittest.TestCase):
    """A peer upgrade must precede the first cross-project publication."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"version": 1, "machineId": "a", "wikiRoot": str(self.root / "wiki"),
                       "stateDir": str(self.root / "state"), "node": "/node",
                       "worker": str(self.root / "runtime/knowledge-flow/worker.mjs"),
                       "projects": {"alpha": {"pages": []}, "beta": {"pages": []}},
                       "exchange": {"protocolVersion": 2, "participants": ["a", "b"],
                                    "root": str(self.root / "exchange")}}
        save_json(self.root / "runtime/build-manifest.json", {"commit": "a" * 40, "capabilities": [CAPABILITY]})
        self.announce("a", "a")

    def announce(self, machine, commit, capabilities=None):
        save_json(self.root / f"exchange/machines/{machine}.json", {"machineId": machine,
                  "protocolVersion": 2, "runtimeCommit": commit * 40,
                  "capabilities": [CAPABILITY] if capabilities is None else capabilities})

    def test_missing_or_legacy_peer_blocks_activation_without_writing(self):
        """Given a legacy peer, When activation is requested, Then no policy is published."""
        self.announce("b", "b", [])
        self.assertFalse(readiness(self.config)["ready"])
        with self.assertRaisesRegex(ValueError, "b"):
            activate(self.config, "2026-09-26T10:00:00Z", apply=True)
        self.assertFalse((self.root / "exchange/v2/topic-scope.json").exists())

    def test_ready_peers_activate_one_shared_policy_and_all_hosts_load_it(self):
        """Given upgraded peers, When one activates, Then shared scope is effective on reads."""
        self.announce("b", "b")
        self.assertEqual(activate(self.config, "2026-09-26T10:00:00Z")["status"], "ready")
        self.assertFalse((self.root / "exchange/v2/topic-scope.json").exists())
        self.assertEqual(activate(self.config, "2026-09-26T10:00:00Z", apply=True)["status"], "enabled")
        cfg = self.root / "config.json"
        save_json(cfg, self.config)
        self.assertEqual(config_from(cfg)["topicScope"], "semantic")
        self.assertEqual(activate(self.config, "2026-09-27T10:00:00Z", apply=True)["status"], "enabled")

    def test_peer_downgrade_blocks_new_semantic_publications(self):
        """Given activation, When a peer advertises old code, Then readiness fails closed."""
        self.announce("b", "b")
        activate(self.config, "2026-09-26T10:00:00Z", apply=True)
        self.announce("b", "c", [])
        with self.assertRaisesRegex(ValueError, "b"):
            require_ready(self.config)

    def test_stale_local_announcement_cannot_activate_new_code(self):
        """An old announcement must not attest a differently installed local runtime."""
        self.announce("a", "c")
        self.announce("b", "b")
        with self.assertRaisesRegex(ValueError, "a"):
            activate(self.config, "2026-09-26T10:00:00Z", apply=True)

    def test_global_catalog_and_legacy_project_catalog_keep_distinct_contracts(self):
        """Given two origins, semantic jobs see both; frozen project jobs stay scoped."""
        folder = self.root / "wiki/wiki/concepts"
        folder.mkdir(parents=True)
        for project in ("alpha", "beta"):
            (folder / f"{project}.md").write_text(f"---\nprojectId: {project}\n---\nContent")
        cfg = {**self.config, "topicScope": "semantic"}
        self.assertEqual(page_ids(cfg, "alpha"), ["concepts/alpha", "concepts/beta"])
        self.assertEqual(page_ids(cfg, "alpha", topic_scope="project"), ["concepts/alpha"])
        (folder / "linked.md").symlink_to(folder / "alpha.md")
        self.assertNotIn("concepts/linked", page_ids(cfg, "alpha"))

    def test_source_projects_keep_a_shared_topic_visible_to_each_origin(self):
        """A semantic page belongs in each source project's optional filtered view."""
        folder = self.root / "wiki/wiki/concepts"
        folder.mkdir(parents=True)
        (folder / "shared.md").write_text("---\ntopicScope: semantic\nsourceProjectIds:\n  - alpha\n  - beta\n---\nContent")
        self.assertEqual(page_ids(self.config, "alpha"), ["concepts/shared"])
        self.assertEqual(page_ids(self.config, "beta"), ["concepts/shared"])

    def test_frozen_legacy_batches_do_not_gain_global_candidates(self):
        """Given activation, When old jobs drain, Then their original project contract survives."""
        folder = self.root / "wiki/wiki/concepts"
        folder.mkdir(parents=True)
        for project in ("alpha", "beta"):
            (folder / f"{project}.md").write_text(f"---\nprojectId: {project}\n---\nContent")
        job = {"id": "old", "projectId": "alpha", "sessionId": "s", "evidence": []}
        semantic = {**job, "id": "new", "topicScope": "semantic"}
        self.assertNotEqual(_batch_key(job), _batch_key(semantic))
        cfg = {**self.config, "topicScope": "semantic"}
        self.assertEqual(_merge_batch([(self.root / "old.json", job)], cfg)["allowedPageIds"], ["concepts/alpha"])
        self.assertEqual(_merge_batch([(self.root / "new.json", semantic)], cfg)["allowedPageIds"],
                         ["concepts/alpha", "concepts/beta"])

    def test_only_fresh_session_jobs_receive_semantic_contract(self):
        """Given semantic activation, When capture repeats, Then queued legacy jobs stay frozen."""
        cfg = {**self.config, "topicScope": "semantic", "sessionConsolidation": {"enabled": True}}
        job = {"id": "new", "projectId": "alpha", "sessionId": "s"}
        queued = self.root / "queue.json"
        prepare_session_job(job, queued, cfg)
        self.assertEqual(job["topicScope"], "semantic")
        save_json(queued, {})
        legacy = {"id": "old", "projectId": "alpha", "sessionId": "s"}
        prepare_session_job(legacy, queued, cfg)
        self.assertNotIn("topicScope", legacy)

    def test_semantic_publication_requires_job_contract_and_shared_activation(self):
        """Given a forged semantic result, Then neither a legacy job nor a local toggle suffices."""
        result = {"contribution": {"topicRevisions": [{"topicScope": "semantic"}]}}
        with self.assertRaisesRegex(ValueError, "semantic job"):
            require_publication(self.config, {}, result)
        job = {"topicScope": "semantic"}
        with self.assertRaisesRegex(ValueError, "shared topic activation"):
            require_publication({**self.config, "topicScope": "semantic"}, job, result)
        self.announce("b", "b")
        activate(self.config, "2026-09-26T10:00:00Z", apply=True)
        require_publication(self.config, job, result)
        self.announce("b", "c", [])
        with self.assertRaisesRegex(ValueError, "b"):
            require_publication(self.config, job, result)

    def test_shared_policy_symlink_is_rejected_without_reading_target(self):
        """A redirected activation file cannot silently widen page scope."""
        self.announce("b", "b")
        target = self.root / "outside.json"
        save_json(target, {"topicScope": "semantic"})
        policy = self.root / "exchange/v2/topic-scope.json"
        policy.parent.mkdir()
        policy.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            activate(self.config, "2026-09-26T10:00:00Z", apply=True)

    def test_operator_cli_previews_and_activates_the_same_policy(self):
        """Given ready readers, the public maintenance CLI previews before its explicit apply."""
        self.announce("b", "b")
        config_file = self.root / "config.json"
        save_json(config_file, self.config)
        command = [sys.executable, "-B", str(Path(__file__).with_name("maintenance.py")),
                   "--config", str(config_file), "--semantic-topics"]
        def invoke(*args):
            result = subprocess.run([*command, *args], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        self.assertEqual(invoke("status")["topicScope"], "project")
        self.assertEqual(invoke("enable")["status"], "ready")
        self.assertFalse((self.root / "exchange/v2/topic-scope.json").exists())
        self.assertEqual(invoke("enable", "--apply")["status"], "enabled")
        self.assertEqual(invoke("status")["topicScope"], "semantic")


if __name__ == "__main__":
    unittest.main()
