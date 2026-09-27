"""Given/When/Then tests for frontmatter-first page ownership discovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from common import digest, page_ids, save_json


class FrontmatterOwnershipTests(unittest.TestCase):
    """Readable names cannot override explicit project metadata."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.config = {"wikiRoot": str(root), "stateDir": str(root / "state"),
                       "projects": {"growth": {"pages": []}}}
        (root / "wiki/concepts").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_metadata_owner_wins_over_filename_prefix(self):
        """Given readable names, When metadata is scanned, Then wrong owners stay hidden."""
        directory = self.root / "wiki/concepts"
        prefix = "growth-" + digest("growth")[:8]
        (directory / (prefix + "-renamed.md")).write_text("---\nprojectId: other\n---\n", encoding="utf-8")
        (directory / "other-readable.md").write_text("---\nprojectId: growth\n---\n", encoding="utf-8")
        self.assertEqual(page_ids(self.config, "growth"), ["concepts/other-readable"])

    def test_legacy_prefix_is_used_only_without_metadata(self):
        """Given an old page without frontmatter, When discovered, Then its prefix remains compatible."""
        prefix = "growth-" + digest("growth")[:8]
        (self.root / "wiki/concepts" / (prefix + "-legacy.md")).write_text("legacy", encoding="utf-8")
        self.assertEqual(page_ids(self.config, "growth"), ["concepts/" + prefix + "-legacy"])

    def test_migrated_registry_and_config_return_only_readable_current_topics(self):
        """Given retired configured/registered pages, When the next job scopes context, Then only its canonical page survives."""
        self.config["projects"]["growth"]["pages"] = ["concepts/retired-config"]
        save_json(self.root / "state/pages.json", {"growth": ["concepts/retired-registry"]})
        (self.root / "wiki/concepts/canonical.md").write_text("---\nprojectId: growth\n---\nCurrent topic")
        self.assertEqual(page_ids(self.config, "growth"), ["concepts/canonical"])

    def test_explicit_legacy_pages_remain_readable_but_foreign_or_symlinked_pages_do_not(self):
        """Given explicit mappings, When ownership disagrees or a path is unsafe, Then configuration cannot expand scope."""
        directory = self.root / "wiki/concepts"
        (directory / "manual.md").write_text("Manual context")
        (directory / "foreign.md").write_text("---\nprojectId: other\n---\nForeign context")
        (directory / "linked.md").symlink_to(directory / "manual.md")
        self.config["projects"]["growth"]["pages"] = ["concepts/manual", "concepts/foreign", "concepts/linked"]
        save_json(self.root / "state/pages.json", {"growth": ["concepts/unowned"]})
        (directory / "unowned.md").write_text("Not an explicit page")
        self.assertEqual(page_ids(self.config, "growth"), ["concepts/manual"])


if __name__ == "__main__":
    unittest.main()
