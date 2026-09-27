"""Given/When/Then tests for citation retirement wire declarations."""

from __future__ import annotations

import unittest

from citation_retirement_contract import validate_citation_retirements
from revision_contract import validate_topic_migration, validate_topic_revisions


def _retirement(citation: str = "^[old:1]", replacement: str = "^[new:2]", reason: str = "合并重复出处") -> dict[str, str]:
    """Build one valid retirement declaration for focused contract tests."""
    return {"citation": citation, "reason": reason, "replacement": replacement}


def _claim() -> dict[str, object]:
    """Build one claim matching the revision fixture's topic identity."""
    return {"targetPageId": "concepts/sample-material-test.md", "topic": "投放", "decisionObject": "样例首轮素材"}


class CitationRetirementContractTests(unittest.TestCase):
    """Retirements fail closed while preserving explicit optional wire data."""

    def test_marker_replacement_and_trimmed_reason_are_preserved(self):
        """Given a complete marker replacement, When validated, Then the trimmed reason survives."""
        value = validate_citation_retirements([_retirement(reason="合并重复出处")], "正文 ^[new:2]")
        self.assertEqual(value, [_retirement(reason="合并重复出处")])

    def test_missing_replacement_in_body_is_rejected(self):
        """Given a replacement absent from the page, When validated, Then it cannot retire evidence."""
        with self.assertRaisesRegex(ValueError, "replacement"):
            validate_citation_retirements([_retirement()], "正文")

    def test_retired_citation_still_in_body_is_rejected(self):
        """Given a citation still present, When validated, Then retirement is rejected."""
        with self.assertRaisesRegex(ValueError, "retained"):
            validate_citation_retirements([_retirement()], "正文 ^[old:1] ^[new:2]")

    def test_item_shape_is_strict_and_citations_are_unique(self):
        """Given unknown fields or duplicate citations, When validated, Then both fail closed."""
        unknown = _retirement()
        unknown["extra"] = "x"
        with self.assertRaisesRegex(ValueError, "fields"):
            validate_citation_retirements([unknown], "正文 ^[new:2]")
        duplicate = [_retirement(), _retirement(reason="另一个理由")]
        with self.assertRaisesRegex(ValueError, "duplicated"):
            validate_citation_retirements(duplicate, "正文 ^[new:2]")

    def test_marker_and_reason_bounds_are_enforced(self):
        """Given malformed marker or reason values, When validated, Then bounds fail closed."""
        invalid_markers = ("^[x]尾", "^[x\n]", "^[x]]", "x", "^[]")
        for citation in invalid_markers:
            with self.subTest(citation=citation), self.assertRaisesRegex(ValueError, "citation"):
                validate_citation_retirements([_retirement(citation=citation)], "正文 ^[new:2]")
        with self.assertRaisesRegex(ValueError, "reason"):
            validate_citation_retirements([_retirement(reason=" ")], "正文 ^[new:2]")
        with self.assertRaisesRegex(ValueError, "reason"):
            validate_citation_retirements([_retirement(reason="r" * 1001)], "正文 ^[new:2]")
        with self.assertRaisesRegex(ValueError, "citation"):
            validate_citation_retirements([_retirement(citation="^[" + "x" * 1022 + "]")], "正文 ^[new:2]")

    def test_revision_placeholder_requires_page_claim_and_body_presence(self):
        """Given a claim replacement, When revision indexes cite it, Then it is accepted."""
        body = "正文 {{claim:2}}"
        value = validate_citation_retirements(
            [_retirement(replacement="{{claim:2}}")], body, claim_indexes=[2])
        self.assertEqual(value[0]["replacement"], "{{claim:2}}")
        with self.assertRaisesRegex(ValueError, "replacement"):
            validate_citation_retirements([_retirement(replacement="{{claim:02}}")], "正文 {{claim:02}}", [2])
        for indexes, replacement, replacement_body in (
            (None, "{{claim:2}}", body), ([1], "{{claim:2}}", body),
            ([2], "{{claim:3}}", "正文 {{claim:3}}")):
            with self.subTest(indexes=indexes, replacement=replacement), self.assertRaisesRegex(ValueError, "claim"):
                validate_citation_retirements([_retirement(replacement=replacement)], replacement_body, indexes)

    def test_migration_cannot_use_claim_placeholder(self):
        """Given a migration page, When replacement uses a claim placeholder, Then it is rejected."""
        with self.assertRaisesRegex(ValueError, "replacement"):
            validate_citation_retirements([_retirement(replacement="{{claim:0}}")], "正文 {{claim:0}}")

    def test_https_url_replacement_rejects_userinfo_invalid_hosts_and_whitespace(self):
        """Given URL replacements, When URL safety fails, Then only clean HTTPS remains valid."""
        valid = "https://例子.测试/docs?q=1"
        self.assertEqual(validate_citation_retirements([_retirement(replacement=valid)], f"正文 {valid}")[0]["replacement"], valid)
        invalid = ("http://example.com/x", "https://user@example.com/x", "https://example.com/a b",
                   "https:///missing-host", "https://bad_host.example/x", "https://example.com:",
                   "https://example.com../x")
        for replacement in invalid:
            with self.subTest(replacement=replacement), self.assertRaisesRegex(ValueError, "replacement"):
                validate_citation_retirements([_retirement(replacement=replacement)], f"正文 {replacement}")

    def test_retirement_count_is_bounded(self):
        """Given more than 500 declarations, When validated, Then the group is rejected."""
        with self.assertRaisesRegex(ValueError, "500"):
            validate_citation_retirements([{}] * 501, "正文")

    def test_revision_and_migration_accept_optional_field(self):
        """Given optional declarations, When high-level wires validate, Then each page keeps them."""
        revision = [{"pageId": "concepts/sample-material-test.md", "topicId": "d" * 64, "title": "投放决策",
                     "topic": "投放", "decisionObject": "样例首轮素材", "basisHash": None,
                     "body": "# 投放\n\n{{claim:0}}\n^[new:2]", "claimIndexes": [0],
                     "citationRetirements": [_retirement()]}]
        normalized = validate_topic_revisions(revision, [_claim()])
        self.assertEqual(normalized[0]["citationRetirements"], revision[0]["citationRetirements"])
        migration = {"version": 1, "basisRecordIds": [], "pages": [{
            "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md",
            "topicId": "d" * 64, "title": "投放决策", "topic": "投放", "decisionObject": "样例首轮素材",
            "body": "# 投放\n\n^[new:2]", "previousPages": [],
            "citationRetirements": [_retirement()]}]}
        result = validate_topic_migration(migration, "b" * 64, [])
        self.assertEqual(result["pages"][0]["citationRetirements"], migration["pages"][0]["citationRetirements"])

    def test_migration_retired_pages_are_strict_and_do_not_overlap_targets(self):
        """Given reviewed whole-page retirements, When migration validates, Then ids stay disjoint."""
        retired = {"projectId": "demo", "pageId": "concepts/old", "sha256": "e" * 64,
                   "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}
        migration = {"version": 1, "basisRecordIds": [], "pages": [{
            "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md",
            "topicId": "d" * 64, "title": "投放决策", "topic": "投放", "decisionObject": "样例首轮素材",
            "body": "# 投放", "previousPages": []}], "retiredPages": [retired]}
        result = validate_topic_migration(migration, "b" * 64, [])
        self.assertEqual(result["retiredPages"], [retired])
        for overlap in ("concepts/sample-material-test.md",):
            broken = {**migration, "retiredPages": [{**retired, "pageId": overlap}]}
            with self.subTest(overlap=overlap), self.assertRaisesRegex(ValueError, "overlaps"):
                validate_topic_migration(broken, "b" * 64, [])

    def test_migration_may_contain_only_retired_pages(self):
        """Given no replacement topics and a non-empty retirement list, When validated, Then it is accepted."""
        retired = {"projectId": "demo", "pageId": "concepts/old", "sha256": "e" * 64,
                   "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}
        migration = {"version": 1, "basisRecordIds": [], "pages": [], "retiredPages": [retired]}
        result = validate_topic_migration(migration, "b" * 64, [])
        self.assertEqual(result["pages"], [])
        empty = {**migration, "retiredPages": []}
        with self.assertRaisesRegex(ValueError, "pages"):
            validate_topic_migration(empty, "b" * 64, [])

    def test_retired_page_rejects_unknown_fields_bad_hash_and_external_reference(self):
        """Given malformed whole-page retirement data, When validated, Then it fails closed."""
        base = {"projectId": "demo", "pageId": "concepts/old", "sha256": "e" * 64,
                "reason": "已由外部记录承接", "externalReference": "https://example.com/pr/9"}
        for field, value, error in (("extra", "x", "fields"), ("sha256", "e" * 63, "sha256"),
                                    ("externalReference", "http://example.com/pr/9", "external"),
                                    ("reason", "  有首尾空白", "reason")):
            broken = {**base, field: value}
            if field == "extra":
                broken = {**base, field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                validate_retired_pages_for_test(broken)

    def test_legacy_wires_do_not_gain_empty_field(self):
        """Given old records without declarations, When normalized, Then bytes remain shape-compatible."""
        revision = [{"pageId": "concepts/sample-material-test.md", "topicId": "d" * 64, "title": "投放决策",
                     "topic": "投放", "decisionObject": "样例首轮素材", "basisHash": None,
                     "body": "{{claim:0}}", "claimIndexes": [0]}]
        normalized_revision = validate_topic_revisions(revision, [_claim()])
        self.assertNotIn("citationRetirements", normalized_revision[0])
        migration = {"version": 1, "basisRecordIds": [], "pages": [{
            "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md",
            "topicId": "d" * 64, "title": "投放决策", "topic": "投放", "decisionObject": "样例首轮素材",
            "body": "# 投放", "previousPages": []}]}
        normalized_migration = validate_topic_migration(migration, "b" * 64, [])
        self.assertNotIn("citationRetirements", normalized_migration["pages"][0])
        self.assertNotIn("retiredPages", normalized_migration)


def validate_retired_pages_for_test(item: dict[str, str]) -> None:
    """Validate one migration envelope to exercise the retired-pages wire."""
    migration = {"version": 1, "basisRecordIds": [], "pages": [{
        "projectId": "demo", "projectLabel": "Demo", "pageId": "concepts/sample-material-test.md",
        "topicId": "d" * 64, "title": "投放决策", "topic": "投放", "decisionObject": "样例首轮素材",
        "body": "# 投放", "previousPages": []}], "retiredPages": [item]}
    validate_topic_migration(migration, "b" * 64, [])


if __name__ == "__main__":
    unittest.main()
