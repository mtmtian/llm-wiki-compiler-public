"""Given/When/Then tests for the explicit pre-manifest projection migration."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from replica_integrity import seal_generation
from shared_files import SharedFiles
from shared_materialize import (MANIFEST, PENDING, migrate_legacy_projection,
                                promote_generation)


class SharedMigrationTests(unittest.TestCase):
    """Migration must be explicit, atomic, recoverable, and byte-preserving."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shared, self.generation = self.root / "shared", self.root / "generation"
        self.state = self.root / "state"
        self.config = {"machineId": "a", "enabled": True, "publishEnabled": True,
                       "stateDir": str(self.state), "sharedWikiRoot": str(self.shared),
                       "exchange": {"materializerMachineId": "a", "participants": ["a", "b"]}}
        self.baseline = {"snapshotId": "b" * 64, "files": [
            {"path": "wiki/MOC.md", "text": "Baseline MOC"},
            {"path": "wiki/index.md", "text": "Baseline index"},
            {"path": "wiki/concepts/base.md", "text": "Human page"},
            {"path": "sources/base.md", "text": "Baseline source"},
        ]}
        for item in self.baseline["files"]:
            self.write(self.shared, item["path"], item["text"])
        self.old = {
            "wiki/concepts/companion-record-" + "a" * 32 + "-0.md": "Old decision page\n",
            "sources/knowledge-flow-old.md": "Old evidence\n",
            "wiki/MOC.md": "Old MOC\n",
            "wiki/index.md": "Old index\n",
        }
        for relative, content in self.old.items():
            self.write(self.shared, relative, content)
        for item in self.baseline["files"]:
            self.write(self.generation, item["path"], item["text"])
        self.write(self.generation, "wiki/MOC.md", "New MOC\n")
        self.write(self.generation, "wiki/index.md", "New index\n")
        self.write(self.generation, "wiki/concepts/companion-topic.md", "New topic\n")
        self.write(self.generation, "sources/knowledge-flow-publication-new.md", "New evidence\n")
        self.write(self.generation, ".llmwiki/replica-response.json", '{"pages": 1, "conflicts": []}')
        seal_generation(self.generation, self.generation.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(root: Path, relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def expected(self, entries=None):
        entries = self.old if entries is None else entries
        return {relative: hashlib.sha256(content.encode()).hexdigest()
                for relative, content in entries.items()}

    def migrate(self, expected=None, dry_run=True, config=None):
        return migrate_legacy_projection(config or self.config, str(self.generation),
                                         self.baseline, self.expected() if expected is None else expected, dry_run)

    def test_bad_hash_or_incomplete_inventory_changes_nothing(self):
        """Given invalid evidence, When migration preflights, Then files and state stay untouched."""
        bad = self.expected()
        bad["sources/knowledge-flow-old.md"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.migrate(bad)
        self.assertFalse(self.state.exists())
        self.assertEqual((self.shared / "sources/knowledge-flow-old.md").read_text(), "Old evidence\n")
        with self.assertRaisesRegex(ValueError, "requires migration"):
            self.migrate({relative: value for relative, value in self.expected().items()
                          if relative != "sources/knowledge-flow-old.md"})
        self.assertFalse(self.state.exists())
        with self.assertRaises(ValueError):
            self.migrate({"wiki/concepts/not-a-record.md": "a" * 64})
        self.assertEqual((self.shared / "wiki/MOC.md").read_text(), "Old MOC\n")

    def test_dry_run_only_returns_path_summary(self):
        """Given reviewed old files, When dry-run runs, Then no state or shared bytes are written."""
        result = self.migrate()
        self.assertEqual(result["status"], "dry-run")
        self.assertIn("wiki/concepts/companion-topic.md", result["writePaths"])
        self.assertIn("sources/knowledge-flow-old.md", result["removePaths"])
        self.assertFalse(self.state.exists())
        legacy_page = self.shared / ("wiki/concepts/companion-record-" + "a" * 32 + "-0.md")
        self.assertEqual(legacy_page.read_text(), "Old decision page\n")

    def test_apply_preserves_old_bytes_and_retry_is_current(self):
        """Given a clean inventory, When applied, Then new pages are visible and originals are recoverable."""
        result = self.migrate(dry_run=False)
        self.assertEqual(result["status"], "current")
        self.assertFalse((self.shared / "sources/knowledge-flow-old.md").exists())
        self.assertEqual((self.shared / "wiki/concepts/companion-topic.md").read_text(), "New topic\n")
        backup = sorted((self.shared / "sources").glob(".llmwiki-preserved-*-*.bak"))
        self.assertTrue(any(path.read_text() == "Old evidence\n" for path in backup))
        self.assertTrue((self.state / MANIFEST).exists())
        self.assertFalse((self.state / PENDING).exists())
        retry = promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual(retry, {"status": "current", "written": 0, "removed": 0, "backups": []})

    def test_interruption_is_recovered_by_normal_promote(self):
        """Given an interrupted apply, When normal promotion retries, Then the same pending plan completes."""
        original = SharedFiles.update

        def fail_legacy_source(files, *args, **kwargs):
            if args[0] == "sources/knowledge-flow-old.md":
                raise OSError("interrupted")
            return original(files, *args, **kwargs)

        with patch.object(SharedFiles, "update", fail_legacy_source):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.migrate(dry_run=False)
        self.assertTrue((self.state / PENDING).exists())
        result = promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual(result["status"], "current")
        self.assertFalse((self.state / PENDING).exists())
        self.assertFalse((self.shared / "sources/knowledge-flow-old.md").exists())

    def test_non_materializer_cannot_migrate(self):
        """Given another participant, When migration is requested, Then it is rejected without reading state."""
        config = {**self.config, "machineId": "b", "stateDir": str(self.root / "peer")}
        result = self.migrate(config=config)
        self.assertEqual(result, {"status": "not-materializer", "machineId": "a"})
        self.assertFalse((self.root / "peer").exists())


if __name__ == "__main__":
    unittest.main()
