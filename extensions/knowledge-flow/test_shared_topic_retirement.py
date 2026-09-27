"""Reviewed baseline merges remove old pages safely and restore them on rollback."""
import json
from pathlib import Path

from shared_materialize import MANIFEST, promote_generation
from test_shared_topic_projection import SharedTopicProjectionTests


class SharedTopicRetirementTests(SharedTopicProjectionTests):
    """Use real sealed generations and shared files for deletion/recovery scenarios."""

    def _migrate(self):
        self.config["topicMigration"] = {"version": 1, "basisRecordIds": [], "pages": [
            {"pageId": "concepts/combined", "previousPages": [{"pageId": "concepts/companion", "sha256": "a" * 64}]}]}
        (self.generation / "wiki/concepts/companion.md").unlink()
        self._write(self.generation, "wiki/concepts/combined.md", "Reviewed combined topic")
        self._seal()
        return promote_generation(self.config, str(self.generation), self.baseline)

    def test_reviewed_merge_retires_baseline_page_and_keeps_backup(self):
        """Given an explicit reviewed merge, When promoted, Then old baseline path disappears with backup."""
        result = self._migrate()
        self.assertFalse((self.shared / "wiki/concepts/companion.md").exists())
        self.assertEqual(result["removed"], 1)
        contents = [(self.shared / name).read_text() for name in result["backups"]]
        self.assertIn("Original topic", contents)
        manifest = json.loads((Path(self.config["stateDir"]) / MANIFEST).read_text())
        self.assertEqual(manifest["retiredBaseline"], ["wiki/concepts/companion.md"])
        self.assertEqual(promote_generation(self.config, str(self.generation), self.baseline)["written"], 0)

    def test_rollback_restores_retired_baseline(self):
        """Given a retired page, When the reviewed migration is rolled back, Then baseline is restored."""
        self._migrate()
        self.config.pop("topicMigration")
        self._write(self.generation, "wiki/concepts/companion.md", "Original topic")
        (self.generation / "wiki/concepts/combined.md").unlink()
        self._seal()
        promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Original topic")
        self.assertFalse((self.shared / "wiki/concepts/combined.md").exists())

    def test_recreated_human_page_blocks_migration_replay(self):
        """Given a human recreated a retired path, When sync runs, Then it preserves that edit and fails."""
        self._migrate()
        self._write(self.shared, "wiki/concepts/companion.md", "Human new content")
        with self.assertRaisesRegex(ValueError, "conflict"):
            promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Human new content")

    def test_explicit_retired_page_receives_baseline_tombstone(self):
        """Given a reviewed external page retirement, When promoted, Then only baseline bytes are removed."""
        self.config["topicMigration"] = {"version": 1, "basisRecordIds": [], "pages": [
            {"pageId": "concepts/combined", "previousPages": []}], "retiredPages": [
            {"projectId": "demo", "pageId": "concepts/companion", "sha256": "a" * 64,
             "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}]}
        (self.generation / "wiki/concepts/companion.md").unlink()
        self._write(self.generation, "wiki/concepts/combined.md", "Reviewed combined topic")
        self._seal()

        result = promote_generation(self.config, str(self.generation), self.baseline)

        self.assertFalse((self.shared / "wiki/concepts/companion.md").exists())
        self.assertEqual(result["removed"], 1)
        manifest = json.loads((Path(self.config["stateDir"]) / MANIFEST).read_text())
        self.assertEqual(manifest["retiredBaseline"], ["wiki/concepts/companion.md"])

    def test_retired_only_migration_stays_removed_until_manifest_revoke(self):
        """Given a retired-only migration, When synced twice and revoked, Then tombstone persists then restores baseline."""
        self.config["topicMigration"] = {"version": 1, "basisRecordIds": [], "pages": [], "retiredPages": [
            {"projectId": "demo", "pageId": "concepts/companion", "sha256": "a" * 64,
             "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}]}
        (self.generation / "wiki/concepts/companion.md").unlink()
        self._seal()
        first = promote_generation(self.config, str(self.generation), self.baseline)
        second = promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual(first["removed"], 1)
        self.assertEqual(second["removed"], 0)
        self.assertFalse((self.shared / "wiki/concepts/companion.md").exists())

        self.config.pop("topicMigration")
        self._write(self.generation, "wiki/concepts/companion.md", "Original topic")
        self._seal()
        promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Original topic")

    def test_retired_only_migration_protects_human_replacement(self):
        """Given a tombstoned page, When a human recreates it, Then replay reports a conflict."""
        self.config["topicMigration"] = {"version": 1, "basisRecordIds": [], "pages": [], "retiredPages": [
            {"projectId": "demo", "pageId": "concepts/companion", "sha256": "a" * 64,
             "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}]}
        (self.generation / "wiki/concepts/companion.md").unlink()
        self._seal()
        promote_generation(self.config, str(self.generation), self.baseline)
        self._write(self.shared, "wiki/concepts/companion.md", "Human replacement")
        with self.assertRaisesRegex(ValueError, "conflict"):
            promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Human replacement")
