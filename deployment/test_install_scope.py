"""Tests for explicit repository-scope migration during installer upgrades."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import install
from install_scope import migrate_repository_exclusions


class ScopeMigrationTests(unittest.TestCase):
    """Only an explicitly named owned checkout may lose its stale exclusion."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "checkout"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/sample-owner/llm-wiki-compiler.git"], check=True)
        self.config = {
            "owners": ["sample-owner"],
            "workingForks": [],
            "stateDir": str(root / "state"),
            "wikiRoot": str(root / "state" / "replica" / "current"),
            "worker": str(root / "runtime" / "knowledge-flow" / "worker.mjs"),
            "excludedPaths": [str(self.repo), str(self.repo.parent), str(root / "custom")],
            "excludedRepos": ["sample-owner/llm-wiki-compiler", "other-owner/keep"],
            "projects": {},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_owned_checkout_is_migrated_and_other_exclusions_remain(self):
        result = migrate_repository_exclusions(self.config, [str(self.repo)])
        self.assertEqual(result["paths"], [str(self.repo)])
        self.assertEqual(result["repos"], ["sample-owner/llm-wiki-compiler"])
        self.assertNotIn(str(self.repo), self.config["excludedPaths"])
        self.assertIn(str(self.repo.parent), self.config["excludedPaths"])
        self.assertIn(str(self.repo.parent / "custom"), self.config["excludedPaths"])
        self.assertEqual(self.config["excludedRepos"], ["other-owner/keep"])
        self.assertEqual(migrate_repository_exclusions(self.config, [str(self.repo)]),
                         {"paths": [], "repos": []})

    def test_existing_custom_repo_exclusions_survive_without_migration(self):
        template = json.loads((Path(__file__).parent / "knowledge-flow.json").read_text())
        self.assertNotIn("excludedRepos", template)
        merged = install.merge_config(template, {"excludedRepos": ["other-owner/keep"]})
        self.assertEqual(merged["excludedRepos"], ["other-owner/keep"])

    def test_subdirectory_does_not_authorize_the_whole_checkout(self):
        child = self.repo / "nested"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "checkout root"):
            migrate_repository_exclusions(self.config, [str(child)])

    def test_runtime_checkout_cannot_be_reopened(self):
        runtime = Path(self.config["worker"]).parent.parent
        runtime.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(runtime)], check=True)
        subprocess.run(["git", "-C", str(runtime), "remote", "add", "origin",
                        "https://github.com/sample-owner/llm-wiki-compiler.git"], check=True)
        with self.assertRaisesRegex(ValueError, "runtime/state"):
            migrate_repository_exclusions(self.config, [str(runtime)])

    def test_repository_containing_runtime_data_cannot_be_reopened(self):
        self.config["stateDir"] = str(self.repo / "private-state")
        with self.assertRaisesRegex(ValueError, "runtime/state"):
            migrate_repository_exclusions(self.config, [str(self.repo)])

    def test_unowned_repository_requires_policy_entry(self):
        subprocess.run(["git", "-C", str(self.repo), "remote", "set-url", "origin",
                        "https://github.com/nashsu/llm-wiki.git"], check=True)
        with self.assertRaisesRegex(ValueError, "owners/workingForks"):
            migrate_repository_exclusions(self.config, [str(self.repo)])


if __name__ == "__main__":
    unittest.main()
