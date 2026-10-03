"""Validation for the reviewed legacy topic-route manifest.

The optional manifest lives in the exchange rather than in the Wiki or local
state.  It is an input to one replica generation only: a missing file means an
empty route set, while malformed or stale bytes fail closed before ``current``
can be switched.
"""

from __future__ import annotations

import datetime as _datetime
import json
from pathlib import Path
from typing import Any

from replica_records import CLAIM_REF, LEDGER_VERSION, canonical, is_hash
from revision_contract import validate_topic_migration
from shared_files import SharedFiles

ROUTE_PATH = "v2/topic-routes.json"
MAX_ROUTE_BYTES = 256 * 1024
MAX_GROUPS = 1000
MAX_REFS = 5000
MAX_TOPIC_LENGTH = 160
MAX_OBJECT_LENGTH = 160


def _text(value: Any, field: str, limit: int) -> str:
    """Normalize one bounded route string and reject ambiguous whitespace."""
    if not isinstance(value, str) or not value or len(value) > limit or value != value.strip():
        raise ValueError(f"topic route {field} is invalid")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise ValueError(f"topic route {field} is invalid")
    return normalized


def _reviewed_at(value: Any) -> str:
    """Require an ISO-8601 timestamp with an explicit timezone."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("topic route reviewedAt is invalid")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("topic route reviewedAt is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("topic route reviewedAt requires a timezone")
    return value


def _records_by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index already verified publications and reject duplicate identities."""
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = record.get("id") if isinstance(record, dict) else None
        if not is_hash(identifier) or identifier in result:
            raise ValueError("topic route records are invalid")
        result[identifier] = record
    return result


def _claim_for(ref: str, records: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Resolve one record/index reference against the verified packet set."""
    if not isinstance(ref, str):
        raise ValueError("topic route claimRef is invalid")
    match = CLAIM_REF.fullmatch(ref)
    if match is None or match.group(1) not in records:
        raise ValueError("topic route claimRef is unknown")
    record_id, raw_index = match.groups()
    payload = records[record_id].get("payload")
    claims = payload.get("claims") if isinstance(payload, dict) else None
    index = int(raw_index)
    if not isinstance(claims, list) or index >= len(claims):
        raise ValueError("topic route claimRef index is invalid")
    claim = claims[index]
    if not isinstance(claim, dict) or "decisionObject" in claim or payload.get("version") == LEDGER_VERSION:
        raise ValueError("topic route claimRef must target a legacy claim")
    return record_id, claim


def _validate_group(value: Any, records: dict[str, dict[str, Any]], refs: set[str]) -> dict[str, Any]:
    """Validate one group and its project ownership against publication claims."""
    if not isinstance(value, dict) or set(value) != {"projectId", "topic", "decisionObject", "claimRefs"}:
        raise ValueError("topic route group fields are invalid")
    project = _text(value["projectId"], "projectId", MAX_TOPIC_LENGTH)
    topic = _text(value["topic"], "topic", MAX_TOPIC_LENGTH)
    decision_object = _text(value["decisionObject"], "decisionObject", MAX_OBJECT_LENGTH)
    claim_refs = value["claimRefs"]
    if not isinstance(claim_refs, list) or not claim_refs:
        raise ValueError("topic route claimRefs must be a non-empty array")
    normalized_refs: list[str] = []
    for ref in claim_refs:
        record_id, claim = _claim_for(ref, records)
        if ref in refs:
            raise ValueError("topic route claimRef is duplicated")
        payload = records[record_id].get("payload")
        if not isinstance(payload, dict) or payload.get("projectId") != project:
            raise ValueError("topic route project does not match claim")
        refs.add(ref)
        normalized_refs.append(ref)
    return {"projectId": project, "topic": topic, "decisionObject": decision_object,
            "claimRefs": sorted(normalized_refs)}


def validate_topic_routes(value: Any, baseline_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and canonicalize an optional reviewed route manifest."""
    if not is_hash(baseline_id) or not isinstance(value, dict):
        raise ValueError("topic route manifest is invalid")
    if (set(value) not in ({"version", "baselineId", "reviewedAt", "groups"},
                           {"version", "baselineId", "reviewedAt", "groups", "migration"})
            or type(value.get("version")) is not int or value["version"] not in (1, 2)):
        raise ValueError("topic route manifest fields are invalid")
    if value["version"] == 1 and "migration" in value:
        raise ValueError("topic route manifest fields are invalid")
    if value["version"] == 2 and "migration" not in value:
        raise ValueError("topic route migration is required")
    if value.get("baselineId") != baseline_id:
        raise ValueError("topic route baseline does not match the pinned snapshot")
    _reviewed_at(value.get("reviewedAt"))
    groups = value.get("groups")
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError("topic route groups exceed the allowed limit")
    indexed = _records_by_id(records)
    seen: set[str] = set()
    normalized = [_validate_group(group, indexed, seen) for group in groups]
    if len(seen) > MAX_REFS:
        raise ValueError("topic route claimRefs exceed the allowed limit")
    if value["version"] == 2:
        validate_topic_migration(value["migration"], baseline_id, records)
    return sorted(normalized, key=lambda group: (
        group["projectId"], group["topic"], group["decisionObject"], canonical(group["claimRefs"])))


def _decode_manifest(encoded: bytes, baseline_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Decode one manifest and return both routes and its optional migration."""
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("topic route manifest is not valid UTF-8 JSON") from error
    routes = validate_topic_routes(value, baseline_id, records)
    migration = None
    if value.get("version") == 2:
        migration = validate_topic_migration(value["migration"], baseline_id, records)
    return {"topicRoutes": routes, "topicMigration": migration}


def load_topic_projection(config: dict[str, Any], baseline_id: str,
                          records: list[dict[str, Any]]) -> dict[str, Any]:
    """Read one optional exchange file and pin routes plus full-vault migration."""
    exchange_root = Path(config["exchange"]["root"])
    if not exchange_root.exists() and not exchange_root.is_symlink():
        return {"topicRoutes": [], "topicMigration": None}
    with SharedFiles(exchange_root) as shared:
        encoded = shared.read(ROUTE_PATH, max_bytes=MAX_ROUTE_BYTES)
    if encoded is None:
        return {"topicRoutes": [], "topicMigration": None}
    return _decode_manifest(encoded, baseline_id, records)


def load_topic_routes(config: dict[str, Any], baseline_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read once through ``SharedFiles`` and return canonical route groups."""
    return load_topic_projection(config, baseline_id, records)["topicRoutes"]
