"""Exercise portable project routing without real business names or paths."""

from __future__ import annotations

import copy
import unittest

import install


def routing_template() -> dict:
    """Return an isolated sample policy for merge-behavior tests."""
    return {"projects": {"sample-project": {
        "topicTerms": ["planning", "release"],
        "paths": ["/template/sample-project", "/template/shared"],
        "pages": [],
    }}}


class RoutingMergeTests(unittest.TestCase):
    """Keep shared sample policy additive while preserving local additions."""

    def test_topic_terms_keep_local_additions_on_install_merge(self):
        template = routing_template()
        existing = copy.deepcopy(template)
        existing["projects"]["sample-project"]["topicTerms"] += ["custom-metric", "quality"]

        merged = install.merge_config(template, existing)

        terms = merged["projects"]["sample-project"]["topicTerms"]
        self.assertEqual(terms[-2:], ["custom-metric", "quality"])
        self.assertEqual(len(terms), len(set(terms)))
        self.assertIn("planning", terms)
        self.assertIn("release", terms)

    def test_private_paths_budget_and_identity_survive_template_merge(self):
        template = routing_template()
        existing = copy.deepcopy(template)
        existing.update({"machineId": "peer-a", "maxDailyJobs": 100,
                         "privatePath": "/private/wiki"})
        existing["projects"]["sample-project"]["paths"] += ["/private/work/sample-project"]

        merged = install.merge_config(template, existing)

        self.assertEqual(merged["machineId"], "peer-a")
        self.assertEqual(merged["maxDailyJobs"], 100)
        self.assertEqual(merged["privatePath"], "/private/wiki")
        paths = merged["projects"]["sample-project"]["paths"]
        self.assertIn("/private/work/sample-project", paths)
        self.assertTrue(set(template["projects"]["sample-project"]["paths"]) <= set(paths))

    def test_template_paths_reach_installed_projects_without_duplicates(self):
        template = routing_template()
        existing = copy.deepcopy(template)
        added = template["projects"]["sample-project"]["paths"][-1]
        existing["projects"]["sample-project"]["paths"].remove(added)

        merged = install.merge_config(template, existing)

        paths = merged["projects"]["sample-project"]["paths"]
        self.assertIn(added, paths)
        self.assertEqual(len(paths), len(set(paths)))

    def test_machine_project_paths_replace_merged_paths_exactly(self):
        template = routing_template()
        merged = install.merge_config(template, copy.deepcopy(template))

        install.apply_machine_overrides(
            merged, {"projectPaths": {"sample-project": ["/private/local-project"]}}
        )

        self.assertEqual(merged["projects"]["sample-project"]["paths"], ["/private/local-project"])


if __name__ == "__main__":
    unittest.main()
