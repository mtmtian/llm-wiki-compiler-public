"""Verify semantic queue ancestry and publication gates against real exchange files.

Legacy jobs keep their project basis; semantic jobs observe all accepted source
projects so sequential cross-project edits are not misclassified as concurrent.
"""

import unittest
from pathlib import Path

from common import load_json
from queue_replica import _pin_basis
from replica import publish_record
import test_replica as replica_fixture


class SemanticQueueTests(unittest.TestCase):
    """Use the existing two-machine on-disk replica fixture without model calls."""

    def setUp(self):
        self.fixture = replica_fixture.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = self.fixture.configs["a"]
        self.root = Path(self.fixture.temp.name)
        self.published = []
        for project in ("project", "other"):
            job, result = self.fixture._submission("a", f"Keep {project} evidence", "review", project, project)
            self.published.append(publish_record(self.config, job, result)["publicationId"])

    def pin(self, semantic):
        """Pin actual publications into a prepared job's durable execution basis."""
        job = {"id": "job", "projectId": "project", **({"topicScope": "semantic"} if semantic else {})}
        audit = {"job": job}
        folder = self.root / "generation"
        for project in ("project", "other"):
            target = folder / f"wiki/concepts/{project}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"---\nprojectId: {project}\n---\nEvidence")
        status = {"fullyVisibleRecordIds": self.published, "generationRoot": str(folder), "digest": "d" * 64}
        file = self.root / "audit.json"
        _pin_basis({**self.config, "topicScope": "semantic"}, audit, file, status)
        return load_json(file)["job"]

    def test_semantic_job_observes_cross_project_ancestry_and_catalog(self):
        """Given two accepted origins, Then a semantic edit observes both before planning."""
        job = self.pin(True)
        self.assertEqual(set(job["basisRecordIds"]), set(self.published))
        self.assertEqual(job["allowedPageIds"], ["concepts/other", "concepts/project"])

    def test_old_job_keeps_project_basis_even_after_global_activation(self):
        """Given an old queued job, Then activation does not widen its review contract."""
        job = self.pin(False)
        self.assertEqual(job["basisRecordIds"], [self.published[0]])
        self.assertEqual(job["allowedPageIds"], ["concepts/project"])

    def test_publication_entrypoint_rejects_unactivated_semantic_revision(self):
        """A direct publisher call cannot evade the all-reader activation gate."""
        job, result = self.fixture._submission("a", "New evidence", "review", "new")
        job["topicScope"] = "semantic"
        result["contribution"]["topicRevisions"] = [{"topicScope": "semantic"}]
        before = sorted(self.fixture.exchange.glob("v2/publications/*/*.json"))
        with self.assertRaisesRegex(ValueError, "shared topic activation"):
            publish_record(self.config, job, result)
        self.assertEqual(sorted(self.fixture.exchange.glob("v2/publications/*/*.json")), before)


if __name__ == "__main__":
    unittest.main()
