"""Given/When/Then tests for the read-only project scope resolver."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import read_routing


class ReadRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.pc = root / "Acme" / "PC"
        self.mobile = root / "Acme" / "mobile"
        self.iaa = root / "Acme" / "iaa"
        self.playground = root / "Playground"
        for path in (self.pc, self.mobile, self.iaa, self.playground):
            path.mkdir(parents=True)
        self.config = self.project_policy()

    def project_policy(self):
        """Build the private platform mapping used by the route scenarios."""
        return {
            "stateDir": str(self.root / "state"),
            "owners": ["mine"],
            "workingForks": [],
            "excludedRepos": [],
            "projects": {
                "acme-growth": {
                    "aliases": ["Acme"],
                    "topicTerms": ["增长", "投放", "ROAS", "复盘"],
                    "requiredTerms": ["PC", "桌面", "Web"],
                    "paths": [str(self.pc)],
                },
                "acme-mobile-growth": {
                    "aliases": ["Acme"],
                    "topicTerms": ["增长", "投放", "ROAS", "复盘"],
                    "requiredTerms": ["移动端", "Android", "iOS", "手机端"],
                    "paths": [str(self.mobile)],
                },
                "acme-product": {
                    "aliases": ["Acme"],
                    "topicTerms": ["SSV", "激励广告", "发奖"],
                    "paths": [str(self.iaa)],
                },
                "playground": {
                    "aliases": ["Playground"],
                    "topicTerms": ["小游戏", "存档", "发布"],
                    "paths": [str(self.playground)],
                },
                "sampleb-growth": {
                    "aliases": ["示例乙", "SampleB"],
                    "topicTerms": ["增长", "投放", "ROAS"],
                },
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def read(self, cwd, prompt, binding=None):
        with patch("routing.git_identity", return_value=(None, None)):
            return read_routing.resolve_read(cwd, prompt, binding, self.config)

    def test_business_cwd_routes_a_natural_question_without_topic_terms(self):
        """Given a trusted PC workspace, When a natural question arrives, Then read scope stays bound."""
        project, reason = self.read(str(self.pc), "这批用户质量为什么变差了？")
        self.assertEqual(project, "acme-growth")
        self.assertEqual(reason, "business-workspace")

    def test_existing_binding_accepts_a_long_followup(self):
        """Given a bound project, When a long follow-up has no fixed keywords, Then it remains readable."""
        project, reason = self.read(str(self.root), "这次方案还有哪些风险，以及下一步应该怎么验证？", "acme-growth")
        self.assertEqual(project, "acme-growth")
        self.assertEqual(reason, "business-continuation")

    def test_unique_alias_does_not_require_topic_terms(self):
        project, reason = self.read(str(self.root), "Playground 现在支持哪些语言？")
        self.assertEqual(project, "playground")
        self.assertEqual(reason, "explicit-business-alias")

    def test_shared_acme_alias_requires_a_platform_or_domain(self):
        project, reason = self.read(str(self.root), "Acme 现在怎么样？")
        self.assertIsNone(project)
        self.assertEqual(reason, "ambiguous-business-domains")

    def test_wiki_operations_cannot_inherit_a_business_binding(self):
        self.assertEqual(self.read(str(self.root), "llmwiki worker 当前状态", "acme-growth"),
                         (None, "operational-question"))

    def test_operational_question_keeps_an_independently_identified_project(self):
        self.assertEqual(self.read(str(self.pc), "llmwiki worker 当前状态", "acme-growth")[0],
                         "acme-growth")
        self.assertEqual(self.read(str(self.root), "Playground Wiki 当前状态", "acme-growth")[0], "playground")

    def test_distinct_brand_aliases_are_ambiguous_even_when_one_has_a_topic_term(self):
        project, reason = self.read(str(self.root), "Playground 和 SampleB 投放怎么安排？")
        self.assertIsNone(project)
        self.assertEqual(reason, "ambiguous-business-domains")

    def test_acme_platforms_do_not_cross_route(self):
        self.assertEqual(self.read(str(self.root), "Acme PC 这周表现如何？")[0], "acme-growth")
        self.assertEqual(self.read(str(self.root), "Acme iOS 这周表现如何？")[0], "acme-mobile-growth")
        self.assertEqual(self.read(str(self.root), "Acme SSV 发奖怎么核对？")[0], "acme-product")

    def test_current_acme_scope_can_switch_on_an_explicit_platform(self):
        self.assertEqual(self.read(str(self.pc), "这次看 iOS 移动端")[0], "acme-mobile-growth")
        self.assertEqual(self.read(str(self.pc), "这次看 SSV 发奖")[0], "acme-product")

    def test_current_acme_scope_rejects_conflicting_platforms(self):
        project, reason = self.read(str(self.pc), "这次同时看 PC 和 iOS 移动端")
        self.assertIsNone(project)
        self.assertEqual(reason, "ambiguous-business-domains")

    def test_bound_acme_scope_can_switch_on_an_explicit_platform(self):
        project, _ = self.read(str(self.root), "这次看 iOS 移动端", "acme-growth")
        self.assertEqual(project, "acme-mobile-growth")

    def test_sampleb_alias_does_not_route_to_acme(self):
        project, _ = self.read(str(self.root), "示例乙投放转化趋势怎么看？")
        self.assertEqual(project, "sampleb-growth")

    def test_explicit_alias_switches_away_from_the_cwd_project(self):
        project, reason = self.read(str(self.pc), "Playground 小游戏如何保留存档？")
        self.assertEqual(project, "playground")
        self.assertEqual(reason, "explicit-business-topic")

    def test_weather_is_not_business_read(self):
        project, reason = self.read(str(self.pc), "今天天气怎么样？")
        self.assertIsNone(project)
        self.assertEqual(reason, "general-question")

    def test_named_project_does_not_override_an_explicit_weather_question(self):
        project, reason = self.read(str(self.root), "Playground 今天天气怎么样？")
        self.assertIsNone(project)
        self.assertEqual(reason, "general-question")

    def test_playground_translation_question_is_not_filtered_as_general(self):
        project, _ = self.read(str(self.root), "Playground 小游戏翻译这句之前，先查当前支持哪些语言")
        self.assertEqual(project, "playground")

    def test_foreign_repo_fails_closed_even_for_a_business_prompt(self):
        with patch("routing.git_identity", return_value=(str(self.root), "other/app")):
            project, reason = read_routing.resolve_read(str(self.root), "Acme PC 投放复盘", None, self.config)
        self.assertIsNone(project)
        self.assertEqual(reason, "third-party-or-unverified-repository")

    def test_excluded_path_fails_closed(self):
        self.config["excludedPaths"] = [str(self.pc)]
        project, reason = self.read(str(self.pc), "这批用户质量为什么变差了？")
        self.assertIsNone(project)
        self.assertEqual(reason, "excluded-path")

    def test_unresolved_api_path_keeps_a_trusted_cwd_scope(self):
        project, reason = self.read(str(self.pc), "请看 /api/foo 的行为")
        self.assertEqual(project, "acme-growth")
        self.assertEqual(reason, "business-workspace")


if __name__ == "__main__":
    unittest.main()
