"""Given explicit topic scope, the wire contract accepts only the semantic marker."""

from __future__ import annotations

import copy
import unittest

from revision_contract import validate_topic_revisions


def _revision(scope: object = ...):
    """Build one complete wire revision with an optional scope field."""
    item = {"pageId": "concepts/sample-material-test.md", "topicId": "d" * 64, "title": "投放决策",
            "topic": "投放", "decisionObject": "样例首轮素材", "basisHash": None,
            "body": "{{claim:0}}", "claimIndexes": [0]}
    if scope is not ...:
        item["topicScope"] = scope
    return [item]


def _claims():
    """Return the destination and identity matching the revision."""
    return [{"targetPageId": "concepts/sample-material-test.md", "topic": "投放", "decisionObject": "样例首轮素材"}]


class SemanticRevisionContractTests(unittest.TestCase):
    """Semantic scope is opt-in and invalid values fail closed."""

    def test_legacy_revision_keeps_the_original_wire_shape(self):
        """Given an omitted scope, When validated, Then legacy bytes stay unmarked."""
        result = validate_topic_revisions(_revision(), _claims())
        self.assertNotIn("topicScope", result[0])

    def test_semantic_revision_is_preserved(self):
        """Given semantic scope, When validated, Then the explicit marker survives."""
        result = validate_topic_revisions(_revision("semantic"), _claims())
        self.assertEqual(result[0]["topicScope"], "semantic")

    def test_null_or_unknown_scope_is_rejected(self):
        """Given any present non-semantic scope, When validated, Then it is rejected."""
        for scope in (None, "project", "SEMANTIC", 1):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "topicScope"):
                validate_topic_revisions(_revision(scope), _claims())
