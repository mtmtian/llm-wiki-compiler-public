"""Review lineage counts active holds without trusting malformed links."""

import tempfile
import unittest
from pathlib import Path

from common import save_json
from current_reviews import current_reviews


class CurrentReviewTests(unittest.TestCase):
    """Exercise review ancestry using real temporary files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def review(self, identifier, project="p", **fields):
        save_json(self.state / "review" / f"{identifier}.json",
                  {"jobId": identifier, "projectId": project, **fields})

    def test_successor_chain_counts_as_one_and_replaced_id_excludes_chain(self):
        self.review("old")
        self.review("retry", reviewRetryOf="old")
        self.review("retry-2", reviewRetryOf="retry")

        self.assertEqual([item[0] for item in current_reviews(self.state, "p")], ["retry-2"])
        self.assertEqual(current_reviews(self.state, "p", replaced_id="old"), [])

    def test_cycles_and_forks_are_counted_independently(self):
        self.review("cycle-a", reviewRetryOf="cycle-b")
        self.review("cycle-b", reviewRetryOf="cycle-a")
        self.review("fork-a")
        self.review("fork-b", reviewRetryOf="fork-a")
        self.review("fork-c", reviewRetryOf="fork-a")

        self.assertEqual(len(current_reviews(self.state, "p")), 5)
        self.assertEqual(len(current_reviews(self.state, "p", replaced_id="fork-a")), 4)

    def test_missing_parent_and_cross_project_links_do_not_fold(self):
        self.review("old", jobId="old")
        self.review("missing-child", reviewRetryOf="absent")
        self.review("other", project="q")
        self.review("cross-child", reviewRetryOf="other")

        self.assertEqual(len(current_reviews(self.state, "p")), 3)

    def test_legacy_record_without_job_id_is_only_a_parent_when_linked(self):
        save_json(self.state / "review" / "legacy.json", {"projectId": "p"})
        self.review("child", reviewRetryOf="legacy")
        self.review("standalone", project="p")

        active = current_reviews(self.state, "p")
        self.assertEqual([item[0] for item in active], ["child", "standalone"])


if __name__ == "__main__":
    unittest.main()
