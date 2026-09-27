"""Strict receipt schema and publication-pair validation for conflict reviews.

Cloud files can arrive independently. Structural validation is unconditional;
semantic coverage checks run only after both immutable publications are present.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from replica_records import is_hash

_ENVELOPE_FIELDS = {"version", "baselineId", "reviewedAt", "resolutions"}
_RESOLUTION_FIELDS = {"recordId", "coveredBy", "claimMappings", "reason"}
_MAPPING_FIELDS = {"claimIndex", "coveredByIndexes"}


def validate_receipt(encoded: bytes, baseline_id: str,
                     records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate all receipt structure and return only currently available pairs."""
    envelope = _decode_receipt(encoded)
    _validate_envelope(envelope, baseline_id)
    return list(available_resolutions(envelope["resolutions"], index_records(records), baseline_id).values())


def _decode_receipt(encoded: bytes) -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate object fields."""
    try:
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("publication resolution receipt is not valid UTF-8 JSON") from error

def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects with repeated keys."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("publication resolution receipt has duplicate fields")
        value[key] = item
    return value

def _validate_envelope(value: Any, baseline_id: str) -> None:
    """Require the exact receipt envelope and an aware review timestamp."""
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        raise ValueError("publication resolution envelope fields are invalid")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("publication resolution version is invalid")
    if not is_hash(baseline_id) or value["baselineId"] != baseline_id:
        raise ValueError("publication resolution baseline does not match")
    if not isinstance(value["resolutions"], list):
        raise ValueError("publication resolutions must be a list")
    reviewed_at = value["reviewedAt"]
    if not isinstance(reviewed_at, str):
        raise ValueError("publication resolution reviewedAt is invalid")
    try:
        timestamp = datetime.datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("publication resolution reviewedAt is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("publication resolution reviewedAt requires a timezone")

def index_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index verified publication packets and reject ambiguous input records."""
    if not isinstance(records, list):
        raise ValueError("publication records must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        if (not isinstance(record, dict) or not is_hash(record.get("id"))
                or not isinstance(record.get("payload"), dict) or record["id"] in indexed):
            raise ValueError("publication records are invalid or duplicated")
        indexed[record["id"]] = record
    return indexed

def _validate_resolution(value: Any, records: dict[str, dict[str, Any]],
                         baseline_id: str | None) -> dict[str, Any]:
    """Validate a structurally checked resolution against both available records."""
    old_id, new_id = value["recordId"], value["coveredBy"]
    old_payload, new_payload = records[old_id]["payload"], records[new_id]["payload"]
    _validate_record_pair(old_payload, new_payload, baseline_id)
    mappings = _validate_mappings(value["claimMappings"], old_payload, new_payload)
    return {"recordId": old_id, "coveredBy": new_id,
            "claimMappings": mappings, "reason": value["reason"]}


def _validate_resolution_shape(value: Any) -> None:
    """Reject malformed receipts even when a referenced cloud packet is delayed."""
    if not isinstance(value, dict) or set(value) != _RESOLUTION_FIELDS:
        raise ValueError("publication resolution fields are invalid")
    old_id, new_id, reason = value["recordId"], value["coveredBy"], value["reason"]
    if not is_hash(old_id) or not is_hash(new_id) or old_id == new_id:
        raise ValueError("publication resolution IDs are invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("publication resolution reason is invalid")
    mappings = value["claimMappings"]
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("publication resolution must cover every old claim")
    for item in mappings:
        _validate_mapping_shape(item)
    indexes = [item["claimIndex"] for item in mappings]
    if sorted(indexes) != list(range(len(mappings))):
        raise ValueError("publication resolution must cover each old claim exactly once")


def _validate_mapping_shape(value: Any) -> None:
    """Check index syntax without requiring the independently delivered records."""
    if not isinstance(value, dict) or set(value) != _MAPPING_FIELDS:
        raise ValueError("publication claim mapping fields are invalid")
    old_index, new_indexes = value["claimIndex"], value["coveredByIndexes"]
    if (type(old_index) is not int or old_index < 0
            or not isinstance(new_indexes, list) or not new_indexes):
        raise ValueError("publication claim mapping indexes are invalid")
    if any(type(index) is not int or index < 0 for index in new_indexes):
        raise ValueError("publication replacement claim index is invalid")
    if len(set(new_indexes)) != len(new_indexes):
        raise ValueError("publication mapping repeats a replacement claim")

def _validate_record_pair(old: dict[str, Any], replacement: dict[str, Any],
                          baseline_id: str | None) -> None:
    """Enforce same-baseline, same-project legacy-to-revision replacement."""
    if (baseline_id is not None and (old.get("baselineId") != baseline_id
                                     or replacement.get("baselineId") != baseline_id)):
        raise ValueError("publication resolution record baseline does not match")
    if not isinstance(old.get("projectId"), str) or old.get("projectId") != replacement.get("projectId"):
        raise ValueError("publication resolution crosses projects")
    if "topicRevisions" in old:
        raise ValueError("resolved publication must be a legacy record")
    revisions = replacement.get("topicRevisions")
    if not isinstance(revisions, list) or not revisions:
        raise ValueError("replacement publication must contain topic revisions")

def _validate_mappings(value: Any, old: dict[str, Any], replacement: dict[str, Any]) -> list[dict[str, Any]]:
    """Require complete old-claim coverage and same-page replacement claims."""
    old_claims, new_claims = old.get("claims"), replacement.get("claims")
    if not isinstance(old_claims, list) or not old_claims or not isinstance(new_claims, list) or not new_claims:
        raise ValueError("publication resolution claims are invalid")
    if not isinstance(value, list) or len(value) != len(old_claims):
        raise ValueError("publication resolution must cover every old claim")
    mappings = [_validate_mapping(item, old_claims, new_claims) for item in value]
    return sorted(mappings, key=lambda item: item["claimIndex"])

def _validate_mapping(value: Any, old_claims: list[Any], new_claims: list[Any]) -> dict[str, Any]:
    """Check one non-empty index mapping and preserve the target page boundary."""
    old_index, new_indexes = value["claimIndex"], value["coveredByIndexes"]
    if old_index >= len(old_claims):
        raise ValueError("publication claim mapping indexes are invalid")
    if any(index >= len(new_claims) for index in new_indexes):
        raise ValueError("publication replacement claim index is invalid")
    old_target = target_page(old_claims[old_index])
    if any(target_page(new_claims[index]) != old_target for index in new_indexes):
        raise ValueError("publication claim mapping crosses target pages")
    return {"claimIndex": old_index, "coveredByIndexes": sorted(new_indexes)}

def target_page(claim: Any) -> str:
    """Return one explicit, non-empty claim target."""
    target = claim.get("targetPageId") if isinstance(claim, dict) else None
    if not isinstance(target, str) or not target.strip():
        raise ValueError("publication claim mapping requires a target page")
    return target

def _validate_resolution_set(resolutions: list[dict[str, Any]]) -> None:
    """Reject duplicate old records and replacement chains in one receipt."""
    old_ids = [item["recordId"] for item in resolutions]
    if len(old_ids) != len(set(old_ids)):
        raise ValueError("publication resolution repeats an old record")
    if any(item["coveredBy"] in old_ids for item in resolutions):
        raise ValueError("publication resolution replacement is also being resolved")

def available_resolutions(resolutions: list[dict[str, Any]], records: dict[str, dict[str, Any]],
                          baseline_id: Any) -> dict[str, dict[str, Any]]:
    """Normalize available resolutions while leaving missing records unresolved."""
    if not isinstance(resolutions, list):
        raise ValueError("publication resolutions must be a list")
    for item in resolutions:
        _validate_resolution_shape(item)
    _validate_resolution_set(resolutions)
    output: list[dict[str, Any]] = []
    for item in resolutions:
        if item["recordId"] not in records or item["coveredBy"] not in records:
            continue
        output.append(_validate_resolution(item, records, baseline_id if is_hash(baseline_id) else None))
    return {item["recordId"]: item for item in output}
