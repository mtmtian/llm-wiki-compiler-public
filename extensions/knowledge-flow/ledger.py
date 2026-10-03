"""Which decided claims stay current once ledger supersessions are applied.

Ledger records (publication ``version`` 3, see deployment/KNOWLEDGE-LEDGER.md
§7) carry accepted claims without page revisions.  A decided claim in one may
name earlier claims it replaces as ``<recordId>:<claimIndex>`` references, the
same form topic routes use.  Packet validation checks each record on its own;
the rules that need other records are decided here, once, for every reader
(the current-decisions digest now, retrieval later) and for the writer's own
check before it publishes.

A reference is valid only when its target is a decided claim of the same
project and decision subject, in a visible record ordered strictly before the
superseding one, which also rules out cycles.  A ledger record with any invalid
reference is rejected as a whole: it neither appears nor hides anything, so a
bad link can never silently remove a current decision.  Superseded claims stay
in the replica and remain traceable.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from replica_records import CLAIM_REF, LEDGER_VERSION


def decision_subject(claim: dict[str, Any]) -> str:
    """The decision object a claim speaks to; claims published without one fall back to their topic."""
    return str(claim.get("decisionObject") or claim.get("topic") or "").strip()


def resolve(payloads: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Return (rejected record ids, superseded claim refs) for visible payloads keyed by record id."""
    rejected: set[str] = set()
    superseded: set[str] = set()
    for record_id, payload in payloads.items():
        if payload.get("version") != LEDGER_VERSION:
            continue
        targets = _targets((record_id, payload), payloads)
        if targets is None:
            rejected.add(record_id)
        else:
            superseded.update(targets)
    return rejected, superseded


def _targets(record: tuple[str, dict[str, Any]], payloads: dict[str, dict[str, Any]]) -> set[str] | None:
    """Every reference the record supersedes, or None when any one of them is invalid."""
    found: set[str] = set()
    for claim in record[1].get("claims", []):
        refs = claim.get("supersedes", []) if isinstance(claim, dict) else []
        if not isinstance(refs, list):
            return None
        for ref in refs:
            if not _valid_target(ref, claim, record, payloads):
                return None
            found.add(ref)
    return found


def _valid_target(ref: Any, claim: dict[str, Any], record: tuple[str, dict[str, Any]],
                  payloads: dict[str, dict[str, Any]]) -> bool:
    """Check one reference against the visible records it may point to."""
    match = CLAIM_REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        return False
    target_id, index = match.group(1), int(match.group(2))
    target = payloads.get(target_id, {})
    claims = target.get("claims")
    if not isinstance(claims, list) or index >= len(claims) or not isinstance(claims[index], dict):
        return False
    record_id, payload = record
    subject = _subject_key(claim)
    return (target.get("projectId") == payload.get("projectId") and claims[index].get("status") == "decided"
            and subject != "" and _subject_key(claims[index]) == subject
            and _order(target_id, target) < _order(record_id, payload))


def _subject_key(claim: dict[str, Any]) -> str:
    """Compare subjects the way topic keys do: Unicode-normalized, whitespace-collapsed, case-folded."""
    return " ".join(unicodedata.normalize("NFC", decision_subject(claim)).split()).casefold()


def _order(record_id: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Publication order used for replay: creation time, then record id."""
    return str(payload.get("createdAt", "")), record_id
