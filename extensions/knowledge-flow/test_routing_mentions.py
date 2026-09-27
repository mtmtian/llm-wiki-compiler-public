"""Given/When/Then coverage for explicit bounded route mentions."""

import tempfile
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import routing
from common import save_json
from route_mentions import parse_route_mentions


class RoutingMentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.growth = root / "work" / "Project With Spaces"
        self.growth.mkdir(parents=True)
        (self.growth / "report.md").write_text("report")
        self.other = root / "other"
        self.other.mkdir()
        self.config = {
            "stateDir": str(root / "state"),
            "owners": ["mine"],
            "workingForks": ["mine/work-fork"],
            "excludedRepos": [],
            "projects": {
                "growth": {"paths": [str(self.growth)], "aliases": ["ProductX"], "topicTerms": ["投放"]},
                "other": {"paths": [str(self.other)], "aliases": ["Other"], "topicTerms": ["代码"]},
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_preserves_delimited_paths_with_spaces(self):
        mentions = parse_route_mentions(
            f"[report]({self.growth / 'report.md'}) `{self.growth}`"
        )
        self.assertIn(str(self.growth / "report.md"), mentions.paths)
        self.assertIn(str(self.growth), mentions.paths)

    def test_non_project_cwd_routes_from_existing_file_mention(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(
                str(self.root), f"请复盘 [{self.growth / 'report.md'}]({self.growth / 'report.md'})", None, self.config
            )
        self.assertEqual(project, "growth")
        self.assertEqual(reason, "explicit-route-mention")

    def test_missing_path_does_not_expand_scope_or_bind(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(
                str(self.root), f"请看 `{self.growth / 'does-not-exist.md'}`", "growth", self.config
            )
        self.assertIsNone(project)
        self.assertEqual(reason, "explicit-path-unresolved")

    def test_api_path_keeps_verified_repository_scope(self):
        """Given an eligible repo, When a prompt mentions an API route, Then its repository scope survives."""
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin",
                        "https://github.com/mine/work-fork.git"], check=True)
        project, reason = routing.resolve(self.root, "修复 /api/jobs 接口，必须保留幂等键", None, self.config)
        self.assertEqual(project, "repo-mine-work-fork")
        self.assertEqual(reason, "owned-repository")

    def test_api_path_keeps_explicit_repository_url_scope(self):
        """Given an eligible repository URL, When an API route is mentioned, Then that repository still binds."""
        project, _ = routing.resolve(self.root, "https://github.com/mine/work-fork 修复 /api/jobs 接口", None, self.config)
        self.assertEqual(project, "repo-mine-work-fork")

    def test_api_path_keeps_explicit_business_scope(self):
        """Given a clear business topic, When an endpoint is mentioned, Then the business still binds."""
        project, _ = routing.resolve(self.root, "ProductX投放需修复 /api/campaign 接口", None, self.config)
        self.assertEqual(project, "growth")

    def test_multiple_project_paths_are_ambiguous(self):
        prompt = f"比较 `{self.growth}` 和 `{self.other}`"
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(str(self.root), prompt, None, self.config)
        self.assertIsNone(project)
        self.assertEqual(reason, "ambiguous-route-mentions")

    def test_overlapping_paths_choose_longest_boundary(self):
        nested = self.growth / "nested"
        nested.mkdir()
        self.config["projects"]["other"]["paths"] = [str(nested)]
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, _ = routing.resolve(str(self.root), f"看 `{nested}`", None, self.config)
        self.assertEqual(project, "other")

    def test_unique_business_topic_can_switch_cwd_default(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(str(self.growth), "Other代码", None, self.config)
        self.assertEqual(project, "other")
        self.assertEqual(reason, "explicit-business-topic")

    def test_github_identity_requires_owner_and_metadata(self):
        save_json(Path(self.config["stateDir"]) / "repo-metadata.json", {
            "mine/app": {"fork": False, "archived": False, "checkedAt": time.time()},
        })
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, _ = routing.resolve(self.root, "请看 https://github.com/mine/app", None, self.config)
        self.assertEqual(project, "repo-mine-app")

    def test_owned_unmapped_cwd_keeps_stable_repo_route(self):
        with patch.object(routing, "git_identity", return_value=("/repo", "mine/unmapped")), \
             patch.object(routing, "eligible_repo", return_value=True):
            project, reason = routing.resolve("/repo", "修复代码", None, self.config)
        self.assertEqual(project, "repo-mine-unmapped")
        self.assertEqual(reason, "owned-repository")

    def test_repo_identity_helper_reads_verified_explicit_url(self):
        save_json(Path(self.config["stateDir"]) / "repo-metadata.json", {
            "mine/app": {"fork": False, "archived": False, "checkedAt": time.time()},
        })
        with patch.object(routing, "git_identity", return_value=(None, None)):
            identity = routing.resolve_repo_identity(
                str(self.root), "https://github.com/mine/app", "repo-mine-app", self.config
            )
        self.assertEqual(identity, "mine/app")

    def test_configured_unique_bare_repo_name_can_assist(self):
        save_json(Path(self.config["stateDir"]) / "repo-metadata.json", {
            "mine/app": {"fork": False, "archived": False, "checkedAt": time.time()},
        })
        self.config["projects"]["app"] = {"repos": ["mine/app"], "paths": []}
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(str(self.root), "请修复 app", None, self.config)
        self.assertEqual(project, "app")
        self.assertEqual(reason, "explicit-route-mention")

    def test_third_party_github_identity_fails_closed(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(
                str(self.root), "请看 https://github.com/other/app", None, self.config
            )
        self.assertIsNone(project)
        self.assertEqual(reason, "third-party-or-unverified-repository")

    def test_short_followups_reuse_binding_but_long_unknown_prompt_does_not(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            for prompt in ("再来一个", "为什么", "不对", "改一下", "这个呢", "是"):
                self.assertEqual(routing.resolve(str(self.root), prompt, "growth", self.config)[0], "growth")
            project, _ = routing.resolve(
                str(self.root), "可以帮我重新设计一套完全不同且需要多步评估的方案", "growth", self.config
            )
        self.assertIsNone(project)

    def test_unmapped_repo_binding_reuses_only_short_followup(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertEqual(routing.resolve(str(self.root), "再来一个", "repo-mine-unmapped", self.config)[0],
                             "repo-mine-unmapped")
            self.assertIsNone(routing.resolve(str(self.root), "请重新设计整个项目方案", "repo-mine-unmapped", self.config)[0])

    def test_excluded_cwd_wins_over_explicit_mention(self):
        self.config["excludedPaths"] = [str(self.root)]
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(
                str(self.root), f"看 `{self.growth}`", None, self.config
            )
        self.assertIsNone(project)
        self.assertEqual(reason, "excluded-path")

    def test_excluded_explicit_path_is_not_a_route(self):
        self.config["excludedPaths"] = [str(self.growth)]
        with patch.object(routing, "git_identity", return_value=(None, None)):
            project, reason = routing.resolve(
                str(self.root), f"看 `{self.growth / 'report.md'}`", None, self.config
            )
        self.assertIsNone(project)
        self.assertEqual(reason, "excluded-path")


if __name__ == "__main__":
    unittest.main()
