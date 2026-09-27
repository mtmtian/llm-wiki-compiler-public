"""Given/When/Then tests for shared immutable publication resolution receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from publication_resolutions import classify_conflicts, load_resolutions
from shared_files import SharedFiles

BASELINE = "c" * 64
OLD_ID = "a" * 64
NEW_ID = "b" * 64
TARGET = "concepts/decision"


def _span(start: int, end: int) -> str:
    """Format the one-based citation range used in generated pages."""
    return str(start) if end == start else f"{start}-{end}"


def _claim(index: int, prefix: str) -> dict:
    """Build one claim with an explicit destination and unique quote."""
    return {"title": f"Choice {index}", "quote": f"{prefix} quote {index}",
            "targetPageId": TARGET}


def _records() -> list[dict]:
    """Return one legacy page publication and its complete revision."""
    old = {"id": OLD_ID, "payload": {"baselineId": BASELINE, "projectId": "growth",
            "claims": [_claim(0, "old"), _claim(1, "old")]}}
    new_claims = [_claim(0, "new"), _claim(1, "new")]
    new_claims[0]["supportingQuotes"] = [{"quote": "supporting quote 0"}]
    replacement = {"id": NEW_ID, "payload": {"baselineId": BASELINE, "projectId": "growth",
                   "projectLabel": "Growth", "createdAt": "2026-09-26T09:00:00Z",
                   "repoIdentity": None, "claims": new_claims,
                   "topicRevisions": [{"pageId": TARGET}]}}
    return [old, replacement]


def _receipt(resolution: dict | None = None) -> dict:
    """Wrap one mapping in the exact versioned receipt envelope."""
    item = resolution or {"recordId": OLD_ID, "coveredBy": NEW_ID,
                          "claimMappings": [{"claimIndex": 0, "coveredByIndexes": [0]},
                                            {"claimIndex": 1, "coveredByIndexes": [1]}],
                          "reason": "Human review confirmed a complete page replacement."}
    return {"version": 1, "baselineId": BASELINE, "reviewedAt": "2026-09-26T10:00:00+00:00",
            "resolutions": [item]}


class PublicationResolutionTests(unittest.TestCase):
    """Receipts only reclassify conflicts after current evidence is verified."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exchange = self.root / "exchange"
        self.exchange.mkdir()
        self.config = {"exchange": {"root": str(self.exchange)}}
        self.records = _records()
        self.generation = self.root / "generation"
        self.receipt_updates = 0
        self._write_generation()

    def _write_receipt(self, envelope: dict):
        """Publish test JSON through the same bounded SharedFiles primitive."""
        encoded = (json.dumps(envelope, separators=(",", ":")) + "\n").encode()
        self._write_bytes(encoded)

    def _write_bytes(self, encoded: bytes):
        """Replace receipt bytes via SharedFiles with a fresh transaction identity."""
        self.receipt_updates += 1
        transaction = hashlib.sha256(encoded + str(self.receipt_updates).encode()).hexdigest()[:32]
        with SharedFiles(self.exchange) as files:
            prior = files.read("v2/publication-resolutions.json")
            files.update("v2/publication-resolutions.json", encoded, prior,
                         transaction, max_bytes=256 * 1024)

    def _write_generation(self, retired: bool = False, retired_support: bool = False):
        """Create current page metadata and its exact indexed quote bundle."""
        payload = self.records[1]["payload"]
        source_name = f"Growth-2026-09-26-{NEW_ID[:12]}.md"
        lines = ["# Growth · 2026-09-26 证据", ""]
        citations, support_markers = [], []
        for index, claim in enumerate(payload["claims"]):
            lines.extend([f"## {index + 1}. {claim['title']}", ""])
            start = len(lines) + 1
            quote_lines = claim["quote"].split("\n")
            lines.extend(quote_lines)
            end = start + len(quote_lines) - 1
            markers = [f"^[{source_name}:{_span(start, end)}]"]
            lines.extend(["", "- Locator: evidence", "- Observed at: 2026-09-26",
                          "- Evidence kind: user", "- Original evidence SHA-256: hash", ""])
            for support in claim.get("supportingQuotes", []):
                lines.append("支持依据：")
                support_start = len(lines) + 1
                support_lines = support["quote"].split("\n")
                lines.extend(support_lines)
                support_end = support_start + len(support_lines) - 1
                marker = f"^[{source_name}:{_span(support_start, support_end)}]"
                markers.append(marker)
                support_markers.append(marker)
                lines.extend(["", "- Locator: evidence", "- Observed at: 2026-09-26",
                              "- Evidence kind: user", "- Original evidence SHA-256: hash", ""])
            citations.extend(markers)
        source = "\n".join(lines)
        refs = [] if retired else [f"{NEW_ID}:0", f"{NEW_ID}:1"]
        frontmatter = ["---", "projectId: growth", "knowledgePublicationRefs:"]
        frontmatter.extend(f'  - "{ref}"' for ref in refs)
        frontmatter.extend(["sources:", f'  - "{source_name}"', "---", ""])
        page_citations = [marker for marker in citations
                          if not retired_support or marker not in support_markers]
        page = "\n".join(frontmatter) + "\n" + " ".join(page_citations)
        for relative, body in (("sources/" + source_name, source), ("wiki/" + TARGET + ".md", page)):
            destination = self.generation / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(body, encoding="utf-8")

    def _status(self, visible: list[str] | None = None) -> dict:
        """Return one raw conflict over the old immutable publication."""
        return {"baselineId": BASELINE, "generationRoot": str(self.generation),
                "fullyVisibleRecordIds": [NEW_ID] if visible is None else visible,
                "conflicts": [{"recordIds": [OLD_ID], "claimRefs": [f"{OLD_ID}:0", f"{OLD_ID}:1"],
                               "reason": "legacy routing conflict"}]}

    def test_valid_whole_page_replacement_moves_conflict_and_preserves_visibility(self):
        """Given a complete accepted replacement, When current citations match, Then only classification changes."""
        self._write_receipt(_receipt())
        resolutions = load_resolutions(self.config, BASELINE, self.records)
        status = self._status()
        before = copy.deepcopy(status)
        result = classify_conflicts(resolutions, status, self.records)
        self.assertEqual(status, before)
        self.assertEqual(result["conflicts"], [])
        resolved = result["resolvedConflicts"][0]
        self.assertEqual(resolved["recordId"], OLD_ID)
        self.assertEqual(resolved["coveredBy"], NEW_ID)
        self.assertEqual(resolved["reason"], "legacy routing conflict")
        self.assertEqual(resolved["resolutionReason"], _receipt()["resolutions"][0]["reason"])
        self.assertEqual(result["fullyVisibleRecordIds"], [NEW_ID])
        conflicting_status = self._status()
        conflicting_status["conflicts"].append({"recordIds": [NEW_ID], "reason": "replacement held"})
        self.assertEqual(classify_conflicts(resolutions, conflicting_status, self.records), conflicting_status)

    def test_missing_held_or_retired_replacement_keeps_original_conflict(self):
        """Given missing, held, or unreferenced replacement evidence, When classified, Then the alert remains active."""
        self._write_receipt(_receipt())
        resolutions = load_resolutions(self.config, BASELINE, self.records)
        expected = self._status()
        missing = classify_conflicts(resolutions, expected, [self.records[0]])
        held = classify_conflicts(resolutions, self._status(visible=[]), self.records)
        self._write_generation(retired=True)
        retired = classify_conflicts(resolutions, expected, self.records)
        self._write_generation(retired_support=True)
        missing_support = classify_conflicts(resolutions, expected, self.records)
        self.assertEqual(missing, expected)
        self.assertEqual(held, self._status(visible=[]))
        self.assertEqual(retired, expected)
        self.assertEqual(missing_support, expected)

    def test_cross_project_partial_coverage_and_bad_index_are_rejected(self):
        """Given a mismatched project or incomplete indexes, When loaded, Then the receipt fails closed."""
        partial = _receipt()
        partial["resolutions"][0]["claimMappings"].pop()
        out_of_range = _receipt()
        out_of_range["resolutions"][0]["claimMappings"][0]["coveredByIndexes"] = [9]
        for envelope in (partial, out_of_range):
            with self.subTest(envelope=envelope):
                self._write_receipt(envelope)
                with self.assertRaises(ValueError):
                    load_resolutions(self.config, BASELINE, self.records)
        cross_target = _records()
        cross_target[1]["payload"]["claims"][0]["targetPageId"] = "concepts/other.md"
        self._write_receipt(_receipt())
        with self.assertRaisesRegex(ValueError, "target pages"):
            load_resolutions(self.config, BASELINE, cross_target)
        self.records[1]["payload"]["projectId"] = "other"
        self._write_receipt(_receipt())
        with self.assertRaisesRegex(ValueError, "projects"):
            load_resolutions(self.config, BASELINE, self.records)

    def test_wrong_baseline_unknown_fields_duplicate_mappings_and_json_keys_are_rejected(self):
        """Given ambiguous or unsupported receipt schema, When loaded, Then it fails closed."""
        wrong_baseline = _receipt()
        wrong_baseline["baselineId"] = "d" * 64
        unknown_field = _receipt()
        unknown_field["futureField"] = True
        duplicate_mapping = _receipt()
        duplicate_mapping["resolutions"][0]["claimMappings"][1]["claimIndex"] = 0
        for envelope in (wrong_baseline, unknown_field, duplicate_mapping):
            with self.subTest(envelope=envelope):
                self._write_receipt(envelope)
                with self.assertRaises(ValueError):
                    load_resolutions(self.config, BASELINE, self.records)
        duplicate_key = json.dumps(_receipt(), separators=(",", ":")).encode()
        self._write_bytes(duplicate_key.replace(b'"version":1', b'"version":1,"version":1', 1))
        with self.assertRaisesRegex(ValueError, "duplicate fields"):
            load_resolutions(self.config, BASELINE, self.records)

    def test_symlinked_receipt_is_rejected(self):
        """Given a symlink at the receipt path, When read through SharedFiles, Then it is rejected."""
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(_receipt()), encoding="utf-8")
        target = self.exchange / "v2/publication-resolutions.json"
        target.parent.mkdir()
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            load_resolutions(self.config, BASELINE, self.records)

    def test_delayed_packets_do_not_relax_receipt_structure_validation(self):
        """Given a packet has not arrived, When receipt syntax is invalid, Then it is still rejected."""
        invalid_id = _receipt()
        invalid_id["resolutions"][0]["coveredBy"] = "not-a-publication-id"
        unknown_mapping = _receipt()
        unknown_mapping["resolutions"][0]["claimMappings"][0]["extra"] = True
        invalid_index = _receipt()
        invalid_index["resolutions"][0]["claimMappings"][0]["coveredByIndexes"] = [True]
        for envelope in (invalid_id, unknown_mapping, invalid_index):
            self._write_receipt(envelope)
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                load_resolutions(self.config, BASELINE, self.records[:1])

    def test_absent_receipt_preserves_status_without_adding_a_field(self):
        """Given no shared receipt, When classified, Then status is copied without schema changes."""
        status = self._status()
        before = copy.deepcopy(status)
        result = classify_conflicts(load_resolutions(self.config, BASELINE, self.records), status, self.records)
        self.assertEqual(result, before)
        self.assertNotIn("resolvedConflicts", result)

    def test_missing_exchange_is_optional_but_dangling_root_symlink_is_rejected(self):
        """Given an absent optional exchange, When loaded, Then no receipt is required and links stay unsafe."""
        self.exchange.rmdir()
        self.assertEqual(load_resolutions(self.config, BASELINE, self.records), [])
        self.exchange.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaises(ValueError):
            load_resolutions(self.config, BASELINE, self.records)


if __name__ == "__main__":
    unittest.main()
