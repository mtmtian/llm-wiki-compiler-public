"""Regression tests for confined baseline paths and diagnostic recovery."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common import load_json
import replica
import shared_files
from replica import initialize_baseline, publish_record, read_baseline, read_records, replica_status
import test_replica


class ReplicaProtocolEdgeTests(unittest.TestCase):
    """Shared baseline recovery must stay inside the configured exchange."""

    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = copy.deepcopy(self.fixture.configs["a"])

    def test_baseline_parent_symlink_is_rejected_for_read_and_initialize(self):
        """A redirected v2 directory cannot make bootstrap write outside exchange."""
        outside = Path(self.fixture.temp.name) / "outside"
        outside.mkdir()
        exchange = Path(self.fixture.temp.name) / "symlink-exchange"
        exchange.mkdir()
        self.config["exchange"]["root"] = str(exchange)
        target = exchange / "v2"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize_baseline({**self.config, "sharedWikiRoot": str(self.fixture.shared)})
        with self.assertRaises(ValueError):
            read_baseline(self.config)
        self.assertFalse((outside / "baseline.json").exists())

    def test_recovered_shared_baseline_clears_invalid_private_cache_diagnostic(self):
        """A valid shared baseline repairs the cache and clears its transient error."""
        cache = Path(self.config["stateDir"]) / "replica-baseline.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{", encoding="utf-8")
        self.assertEqual(read_records(self.config), [])
        errors = replica_status(self.config)["errors"]
        self.assertFalse(any(item.get("error") == "InvalidCachedBaseline" for item in errors))
        self.assertEqual(load_json(cache)["snapshotId"], read_baseline(self.config)["snapshotId"])

    def test_baseline_cumulative_bound_rejects_before_reading_large_next_file(self):
        huge = self.fixture.shared / "sources" / "huge.md"
        huge.write_bytes(b"x" * (1024 * 1024))
        read_bytes = 0
        original_read = shared_files.os.read

        def track_read(descriptor, size):
            nonlocal read_bytes
            block = original_read(descriptor, size)
            read_bytes += len(block)
            return block

        with patch.object(replica, "MAX_BASELINE_BYTES", 27), patch.object(shared_files.os, "read", track_read):
            with self.assertRaisesRegex(ValueError, "maximum"):
                replica._collect_baseline_files(self.fixture.shared)
        self.assertLess(read_bytes, huge.stat().st_size)

    def test_publication_write_pins_descriptor_when_participant_becomes_symlink(self):
        outside = Path(self.fixture.temp.name) / "outside-write"
        outside.mkdir()
        folder = Path(self.config["exchange"]["root"]) / "v2/publications/a"
        original_folder = folder.with_name("a-original")
        job, result = self.fixture._submission("a", "Race write", "always", "race-write")
        original_open = shared_files._open_dir
        swapped = False

        def replace_after_open(parent_fd, name):
            nonlocal swapped
            child = original_open(parent_fd, name)
            if name == "a" and not swapped:
                folder.rename(original_folder)
                folder.symlink_to(outside, target_is_directory=True)
                swapped = True
            return child

        with patch.object(shared_files, "_open_dir", replace_after_open):
            published = publish_record(self.config, job, result)
        self.assertTrue(swapped)
        self.assertTrue((original_folder / f"{published['publicationId']}.json").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_publication_read_pins_descriptor_when_participant_becomes_symlink(self):
        outside = Path(self.fixture.temp.name) / "outside-read"
        outside.mkdir()
        job, result = self.fixture._submission("a", "Race read", "always", "race-read")
        published = publish_record(self.config, job, result)
        folder = Path(self.config["exchange"]["root"]) / "v2/publications/a"
        original_folder = folder.with_name("a-original")
        original_open = shared_files._open_dir
        swapped = False

        def replace_after_open(parent_fd, name):
            nonlocal swapped
            child = original_open(parent_fd, name)
            if name == "a" and not swapped:
                folder.rename(original_folder)
                folder.symlink_to(outside, target_is_directory=True)
                swapped = True
            return child

        with patch.object(shared_files, "_open_dir", replace_after_open):
            records = read_records(self.config)
        self.assertTrue(swapped)
        self.assertEqual([item["id"] for item in records], [published["publicationId"]])
        self.assertFalse(list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
