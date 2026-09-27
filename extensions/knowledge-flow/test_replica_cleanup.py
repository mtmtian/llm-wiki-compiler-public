"""Safety and retention tests for private replica generation cleanup."""

import tempfile
import unittest
import fcntl
from pathlib import Path
from unittest.mock import patch

from common import load_json, save_json
from replica_cleanup import cleanup_generations
from replica import sync_replica


class ReplicaCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.generations = self.state / "replica/generations"
        self.generations.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def generation(self, letter):
        path = self.generations / (letter * 64)
        path.mkdir()
        (path / "marker").write_text(letter, encoding="utf-8")
        return path

    def current(self, path):
        link = self.state / "replica/current"
        link.unlink(missing_ok=True)
        link.symlink_to(path)
        return link

    def test_retains_current_rollback_and_active_frozen_batch(self):
        old, rollback, active, stale = (self.generation(letter) for letter in "abcd")
        current = self.current(active)
        save_json(self.state / "batches/frozen.json", {
            "status": "sync-retry", "replicaBasis": {"wikiRoot": str(old)}})
        result = cleanup_generations(self.state, current, rollback)
        self.assertEqual(set(result["kept"]), {active.name, rollback.name, old.name})
        self.assertTrue(old.exists())
        self.assertTrue(rollback.exists())
        self.assertTrue(active.exists())
        self.assertFalse(stale.exists())

    def test_repeated_switches_remain_bounded_and_keep_previous(self):
        roots = []
        for index in range(5):
            root = self.generation(chr(ord("a") + index))
            rollback = roots[-1] if roots else None
            cleanup_generations(self.state, root, rollback)
            roots.append(root)
        kept = [path for path in self.generations.iterdir() if path.is_dir() and not path.is_symlink()]
        self.assertEqual({path.name for path in kept}, {roots[-1].name, roots[-2].name})

    def test_corrupt_active_audit_skips_deletion(self):
        current = self.generation("a")
        stale = self.generation("b")
        self.current(current)
        batches = self.state / "batches"
        batches.mkdir()
        (batches / "broken.json").write_text("{", encoding="utf-8")
        result = cleanup_generations(self.state, current)
        self.assertIn("error", result)
        self.assertTrue(stale.exists())
        self.assertTrue((self.state / "replica-errors/generation-cleanup.json").exists())

    def test_cleanup_failure_keeps_current_valid_and_records_error(self):
        current = self.generation("a")
        stale = self.generation("b")
        link = self.current(current)
        with patch("replica_cleanup.shutil.rmtree", side_effect=OSError("locked")):
            result = cleanup_generations(self.state, link, current)
        self.assertIn("error", result)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), current.resolve())
        self.assertTrue(stale.exists())
        self.assertTrue(load_json(self.state / "replica-errors/generation-cleanup.json"))
        cleanup_generations(self.state, link, current)
        self.assertFalse((self.state / "replica-errors/generation-cleanup.json").exists())

    def test_hash_symlink_is_not_followed_or_deleted(self):
        current = self.generation("a")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.generations / ("b" * 64)
        link.symlink_to(outside, target_is_directory=True)
        self.current(current)
        cleanup_generations(self.state, current)
        self.assertTrue(link.is_symlink())
        self.assertTrue(outside.exists())

    def replica_config(self):
        root = self.state
        state = root / "machine"
        worker = root / "worker.js"
        (root / "shared").mkdir(parents=True, exist_ok=True)
        return {"machineId": "a", "stateDir": str(state), "worker": str(worker),
                "wikiRoot": str(state / "replica/current"), "sharedWikiRoot": str(root / "shared"),
                "exchange": {"protocolVersion": 2, "root": str(root / "exchange"), "participants": ["a"]}}

    def pin_under_lock(self, config, status):
        state = Path(config["stateDir"])
        with (state / "replica.lock").open("a") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save_json(state / "batches/active.json", {
            "status": "claimed", "replicaBasis": {"wikiRoot": status["generationRoot"]}})

    def test_consumer_pin_is_durable_before_another_sync_can_prune(self):
        config = self.replica_config()
        baseline = {"snapshotId": "a" * 64, "files": []}
        materialize = lambda stage, records: {"pages": 0, "conflicts": []}
        with patch("replica_generation._read_baseline_cached", return_value=baseline), \
                patch("replica_generation.read_records", return_value=[]):
            Path(config["worker"]).write_text("version 1")
            first = sync_replica(config, materialize, after_sync=lambda status: self.pin_under_lock(config, status))
            for version in (2, 3, 4):
                Path(config["worker"]).write_text(f"version {version}")
                sync_replica(config, materialize)
            self.assertTrue(Path(first["generationRoot"]).is_dir())
            generations = Path(config["stateDir"]) / "replica/generations"
            self.assertEqual(len(list(generations.iterdir())), 3)
            save_json(Path(config["stateDir"]) / "batches/active.json", {"status": "completed"})
            sync_replica(config, materialize)
            self.assertFalse(Path(first["generationRoot"]).exists())
            self.assertEqual(len(list(generations.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
