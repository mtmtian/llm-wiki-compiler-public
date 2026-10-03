"""Archiving a repository stops new knowledge but keeps its published history readable.

Given an owned, non-fork repository that was later archived, the local replica
still reads its earlier publications, while intake and shared-proposal review
keep refusing new work for it. Forks and explicitly excluded repositories stay
refused everywhere.
"""

import tempfile
import time
import unittest
from pathlib import Path

from common import save_json
from exchange import incoming_job
from replica import _project_is_allowed
from routing import eligible_repo


class ArchivedHistoryTests(unittest.TestCase):
    """Use the real metadata cache instead of calling GitHub."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        now = time.time()
        save_json(state / "repo-metadata.json", {
            "mine/old": {"fork": False, "archived": True, "checkedAt": now},
            "mine/fork": {"fork": True, "archived": True, "checkedAt": now},
            "mine/hidden": {"fork": False, "archived": True, "checkedAt": now}})
        self.config = {"stateDir": str(state), "owners": ["mine"], "excludedRepos": ["mine/hidden"],
                       "projects": {}, "wikiRoot": str(state), "gh": "false"}

    def payload(self, identity):
        return {"projectId": "repo-" + identity.replace("/", "-"), "repoIdentity": identity}

    def test_archived_repo_is_refused_for_new_work_but_allowed_for_history(self):
        self.assertFalse(eligible_repo("mine/old", self.config))
        self.assertTrue(eligible_repo("mine/old", self.config, allow_archived=True))

    def test_forks_and_excluded_repos_stay_refused_even_for_history(self):
        for identity in ("mine/fork", "mine/hidden"):
            self.assertFalse(eligible_repo(identity, self.config, allow_archived=True))
            self.assertFalse(_project_is_allowed(self.payload(identity), self.config))

    def test_replica_reads_history_while_shared_proposals_are_refused(self):
        """The replica keeps published knowledge; a new shared proposal is not re-reviewed."""
        self.assertTrue(_project_is_allowed(self.payload("mine/old"), self.config))
        packet = {"id": "p" * 64, "payload": {**self.payload("mine/old"), "projectLabel": "old", "machineId": "peer",
                                              "createdAt": "2026-10-03T00:00:00Z", "evidence": [], "claims": []}}
        with self.assertRaises(ValueError):
            incoming_job(packet, self.config)


if __name__ == "__main__":
    unittest.main()
