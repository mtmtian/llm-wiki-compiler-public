"""Behavior tests for reusing baseline topic pages in shared v2 projection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from replica_integrity import seal_generation
from shared_materialize import MANIFEST, promote_generation


class SharedTopicProjectionTests(unittest.TestCase):
    """Keep baseline topic ownership checks independent from record transport."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.shared, self.generation = root / "shared", root / "generation"
        self.config = {
            "machineId": "a", "enabled": True, "publishEnabled": True,
            "stateDir": str(root / "state"), "sharedWikiRoot": str(self.shared),
            "exchange": {"materializerMachineId": "a", "participants": ["a"]},
        }
        self.baseline = {"snapshotId": "b" * 64, "files": [
            {"path": "wiki/MOC.md", "text": "Original MOC"},
            {"path": "wiki/index.md", "text": "Original index"},
            {"path": "wiki/concepts/companion.md", "text": "Original topic"},
            {"path": "sources/base.md", "text": "Original source"},
        ]}
        for item in self.baseline["files"]:
            self._write(self.shared, item["path"], item["text"])
            self._write(self.generation, item["path"], item["text"])
        self._write(self.generation, ".llmwiki/replica-response.json", '{"pages": 1, "conflicts": []}')

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write(root: Path, relative: str, content: str) -> None:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _seal(self) -> None:
        seal_generation(self.generation, self.generation.name)

    def test_updated_baseline_topic_is_published_with_ownership(self):
        """Given a changed topic page, When promoted, Then shared bytes and hash update."""
        self._write(self.generation, "wiki/concepts/companion.md", "Updated topic")
        self._seal()

        result = promote_generation(self.config, str(self.generation), self.baseline)

        self.assertEqual(result["status"], "current")
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Updated topic")
        manifest = json.loads((Path(self.config["stateDir"]) / MANIFEST).read_text())
        self.assertIn("wiki/concepts/companion.md", manifest["files"])

    def test_human_baseline_topic_edit_blocks_before_other_writes(self):
        """Given a human edit, When a topic update arrives, Then the batch is blocked."""
        self._write(self.shared, "wiki/concepts/companion.md", "Human edit")
        self._write(self.generation, "wiki/concepts/companion.md", "Updated topic")
        self._write(self.generation, "wiki/concepts/new.md", "New topic")
        self._seal()

        with self.assertRaisesRegex(ValueError, "conflict"):
            promote_generation(self.config, str(self.generation), self.baseline)
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Human edit")
        self.assertFalse((self.shared / "wiki/concepts/new.md").exists())

    def test_retracted_baseline_topic_restores_original_bytes(self):
        """Given an owned topic, When its contribution is retracted, Then baseline returns."""
        self._write(self.generation, "wiki/concepts/companion.md", "Updated topic")
        self._seal()
        promote_generation(self.config, str(self.generation), self.baseline)

        self._write(self.generation, "wiki/concepts/companion.md", "Original topic")
        self._seal()
        result = promote_generation(self.config, str(self.generation), self.baseline)

        self.assertEqual(result["status"], "current")
        self.assertEqual((self.shared / "wiki/concepts/companion.md").read_text(), "Original topic")

    def test_baseline_source_update_is_ignored(self):
        """Given a changed baseline source, When promoted, Then shared source stays intact."""
        self._write(self.generation, "sources/base.md", "Tampered source")
        self._seal()

        result = promote_generation(self.config, str(self.generation), self.baseline)

        self.assertEqual(result["status"], "current")
        self.assertEqual((self.shared / "sources/base.md").read_text(), "Original source")


if __name__ == "__main__":
    unittest.main()
