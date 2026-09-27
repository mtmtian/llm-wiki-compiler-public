"""Given/When/Then checks for routing configuration in replica cache identity."""

import copy
import unittest
from pathlib import Path

import test_replica
from replica import sync_replica


class TopicGenerationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_replica.ReplicaTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = copy.deepcopy(self.fixture.configs["b"])
        self.config["exchange"]["materializerMachineId"] = "a"

    def materialize(self, stage, records):
        owners = sorted(project for project, value in self.config["projects"].items()
                        if "concepts/base" in value.get("pages", []))
        (Path(stage) / "wiki/concepts/base.md").write_text("# Base\nOwner: " + ",".join(owners))
        return {"pages": 1, "conflicts": []}

    def test_changed_page_ownership_rebuilds_the_visible_view(self):
        """Given cached pages, When ownership changes, Then the visible view uses the new owner."""
        self.config["projects"]["project"]["pages"] = ["concepts/base"]
        first = sync_replica(self.config, self.materialize)
        self.config["projects"]["project"]["pages"] = []
        self.config["projects"]["other"]["pages"] = ["concepts/base"]
        second = sync_replica(self.config, self.materialize)
        self.assertEqual(Path(second["generationRoot"], "wiki/concepts/base.md").read_text(), "# Base\nOwner: other")
        self.assertNotEqual(first["generationRoot"], second["generationRoot"])

    def test_mapping_order_and_labels_do_not_change_the_visible_generation(self):
        """Given equivalent ownership, When order or labels change, Then the view stays identical."""
        self.config["projects"]["project"]["pages"] = ["concepts/base", "concepts/second"]
        first = sync_replica(self.config, self.materialize)
        self.config["projects"] = dict(reversed(list(self.config["projects"].items())))
        self.config["projects"]["project"]["pages"].reverse()
        self.config["projects"]["project"]["label"] = "Renamed display label"
        second = sync_replica(self.config, self.materialize)
        self.assertEqual(first["generationRoot"], second["generationRoot"])
        self.assertEqual(Path(second["generationRoot"], "wiki/concepts/base.md").read_text(), "# Base\nOwner: project")


if __name__ == "__main__":
    unittest.main()
