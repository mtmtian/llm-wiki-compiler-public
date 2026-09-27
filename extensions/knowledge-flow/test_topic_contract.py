"""Given/When/Then contracts for optional decision-object transport."""

from __future__ import annotations

import unittest

from common import digest
from replica_records import canonical, validate_packet


BASELINE_ID = "b" * 64


def claim(**overrides):
    value = {
        "text": "首轮先验证视频访问",
        "evidenceId": "e1",
        "quote": "首轮先验证视频访问",
        "title": "样例素材推广",
        "topic": "样例素材推广",
        "slug": "sample-material-promotion",
        "targetPageId": None,
        "kind": "decision",
        "status": "decided",
        "useWhen": "启动样例首轮素材推广时",
        "rationale": "保留访问优先的取舍",
    }
    value.update(overrides)
    return value


def packet(claim_value):
    text = claim_value["quote"]
    evidence = {
        "id": "e1",
        "kind": "user",
        "text": text,
        "sha256": digest(text),
        "originalSha256": digest("original:" + text),
        "observedAt": "2026-09-17T00:00:00Z",
        "locator": "knowledge-evidence://a/" + digest("turn:e1"),
    }
    payload = {
        "version": 2,
        "baselineId": BASELINE_ID,
        "machineId": "a",
        "projectId": "companion",
        "projectLabel": "Companion",
        "createdAt": "2026-09-17T00:00:00Z",
        "originJobHash": "c" * 64,
        "repoIdentity": None,
        "basisRecordIds": [],
        "claims": [claim_value],
        "evidence": [evidence],
        "review": {"status": "accepted", "model": "test"},
    }
    return {"id": digest(canonical(payload)), "payload": payload}


class TopicContractTests(unittest.TestCase):
    def test_legacy_claim_without_decision_object_remains_replayable(self):
        """Given a legacy publication, When replayed, Then absent metadata remains valid."""
        validate_packet(packet(claim()), "a", BASELINE_ID)

    def test_valid_decision_object_is_preserved(self):
        """Given valid topic identity metadata, When validated, Then it is accepted."""
        validate_packet(packet(claim(decisionObject="样例项目首轮素材测试")), "a", BASELINE_ID)

    def test_explicit_invalid_decision_object_is_rejected(self):
        """Given explicit invalid metadata, When validated, Then transport fails closed."""
        for value in (None, "", " ", 42, "x" * 161):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "decisionObject"):
                    invalid = packet(claim(decisionObject=value))
                    validate_packet(invalid, "a", BASELINE_ID)


if __name__ == "__main__":
    unittest.main()
