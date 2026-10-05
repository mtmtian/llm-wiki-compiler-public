"""Build a sealed, read-only projection of reviewed ledger claims.

The projection gives version 3 claims a searchable local home before their
topic pages exist. It applies the ledger's shared supersession rules, excludes
materializer conflicts, and keeps only metadata for replaced version 2 claims
so readers can suppress retired page conclusions without restoring old prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ledger import decision_subject, resolve
from replica_records import LEDGER_VERSION, PUBLICATION_VERSION

READ_PROJECTION_VERSION = 1
PROJECTION_PATH = Path(".llmwiki") / "reviewed-claims.json"


def build_reviewed_claims_projection(records: list[dict[str, Any]], conflicts: list[Any],
                                    generation_id: str) -> dict[str, Any]:
    """Return the deterministic read view for one materialized generation.

    Args:
        records: Packets already validated by the replica reader for this sync.
        conflicts: The materializer's response conflicts for the same records.
        generation_id: Identity of the generation that will contain the view.

    Returns:
        A versioned envelope containing visible v3 claims, superseded v2
        metadata, and record ids held by conflicts or invalid ledger links.
    """
    conflicted = _conflicted_record_ids(conflicts)
    indexed = {item["id"]: item["payload"] for item in records}
    available = {record_id: payload for record_id, payload in indexed.items()
                 if record_id not in conflicted}
    rejected, superseded = resolve(available)
    accepted = {record_id: payload for record_id, payload in available.items()
                if record_id not in rejected}
    claims, history = _project_claims(accepted, superseded)
    rejected_ids = sorted((conflicted & indexed.keys()) | rejected)
    return {"version": READ_PROJECTION_VERSION, "generationId": generation_id,
            "claims": claims, "superseded": history, "rejectedRecordIds": rejected_ids}


def write_reviewed_claims_projection(stage: Path, records: list[dict[str, Any]],
                                     conflicts: list[Any], generation_id: str) -> dict[str, Any]:
    """Write the read view before generation sealing and return its value."""
    value = build_reviewed_claims_projection(records, conflicts, generation_id)
    destination = Path(stage) / PROJECTION_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    destination.write_text(encoded, encoding="utf-8")
    return value


def _conflicted_record_ids(conflicts: list[Any]) -> set[str]:
    """Collect record ids withheld by materializer conflict groups."""
    result: set[str] = set()
    for conflict in conflicts:
        record_ids = conflict.get("recordIds") if isinstance(conflict, dict) else None
        if isinstance(record_ids, list):
            result.update(item for item in record_ids if isinstance(item, str))
    return result


def _project_claims(payloads: dict[str, dict[str, Any]],
                    superseded: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project v3 claims and only the metadata for superseded v2 claims."""
    claims: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    visible = [(record_id, payload) for record_id, payload in payloads.items()]
    for record_id, payload in sorted(visible, key=_record_order):
        for index, claim in enumerate(payload["claims"]):
            claim_ref = f"{record_id}:{index}"
            item = _claim_item(record_id, payload, index, claim, claim_ref in superseded)
            if payload["version"] == LEDGER_VERSION:
                claims.append(item)
            elif payload["version"] == PUBLICATION_VERSION and claim_ref in superseded:
                history.append(item)
    return claims, history


def _record_order(item: tuple[str, dict[str, Any]]) -> tuple[str, str]:
    """Sort packets by source creation time and then their stable identity."""
    record_id, payload = item
    return str(payload.get("createdAt", "")), record_id


def _claim_item(record_id: str, payload: dict[str, Any], index: int,
                claim: dict[str, Any], is_superseded: bool) -> dict[str, Any]:
    """Copy public claim fields and exact packet evidence into one projection item."""
    evidence_by_id = {item["id"]: item for item in payload["evidence"]}
    evidence_refs = [(claim["evidenceId"], claim["quote"])]
    evidence_refs.extend((item["evidenceId"], item["quote"])
                         for item in claim.get("supportingQuotes", []))
    quotes = _quote_items(evidence_refs, evidence_by_id)
    return {"claimRef": f"{record_id}:{index}", "recordId": record_id,
            "projectId": payload["projectId"], "projectLabel": payload["projectLabel"],
            "title": claim["title"], "topic": claim["topic"],
            "decisionObject": decision_subject(claim), "text": claim["text"],
            "kind": claim["kind"], "status": claim["status"],
            "useWhen": claim["useWhen"], "rationale": claim["rationale"],
            "recordedAt": payload["createdAt"], "targetPageId": claim.get("targetPageId"),
            "superseded": is_superseded, "quotes": quotes}


def _quote_items(references: list[tuple[str, str]],
                 evidence_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the primary quote first and deduplicate exact source evidence ids."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_id, quote in references:
        if evidence_id in seen:
            continue
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None or evidence.get("text") != quote:
            raise ValueError("reviewed claim quote is not backed by packet evidence")
        item = {"evidenceId": evidence_id, "kind": evidence["kind"], "quote": quote,
                "locator": evidence["locator"], "observedAt": evidence["observedAt"],
                "sha256": evidence["sha256"]}
        if isinstance(evidence.get("originalSha256"), str):
            item["originalSha256"] = evidence["originalSha256"]
        result.append(item)
        seen.add(evidence_id)
    return result
