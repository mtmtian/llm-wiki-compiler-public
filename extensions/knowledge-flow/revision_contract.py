"""Wire contracts for reviewed topic revisions, the one-time vault migration and topic merges.

Topic revisions are complete page candidates.  They carry no frontmatter and
refer to immutable publication claims by index, allowing the TypeScript
materializer to edit one coherent topic page while preserving source evidence.
The migration envelope is a reviewed snapshot of old pages and never replaces
the immutable baseline or publication records.
"""

from __future__ import annotations

import datetime as _datetime
import re
from typing import Any

from replica_records import canonical, is_hash

PAGE_ID = re.compile(r"^concepts/[^/\\:\x00]+$")
PLACEHOLDER = re.compile(r"\{\{claim:([0-9]+)\}\}")
MAX_REVISIONS = 5
MAX_BODY = 12000
MAX_PAGES = 1000
MAX_MERGES = 256
MAX_ABSORBED = 1024


def _bounded_text(value: Any, field: str, limit: int = 160) -> str:
    """Require one non-empty, trimmed, bounded identity string."""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        raise ValueError(f"topic revision {field} is invalid")
    return value


def _page_id(value: Any, field: str = "pageId") -> str:
    """Keep page destinations to one safe concepts file."""
    result = _bounded_text(value, field, 256)
    component = result.split("/", 1)[1] if result.count("/") == 1 else ""
    if (not PAGE_ID.fullmatch(result) or component in ("", ".", "..")
            or component.startswith(".") or component.endswith(".")
            or any(char in component for char in "/\\\x00")
            or any(ord(char) < 32 or ord(char) == 127 for char in component)):
        raise ValueError(f"topic revision {field} is invalid")
    return result


def _body(value: Any, field: str = "body") -> str:
    """Reject frontmatter and oversized or empty page bodies."""
    if not isinstance(value, str) or not value.strip() or not 1 <= len(value) <= MAX_BODY:
        raise ValueError(f"topic revision {field} is invalid")
    if value.lstrip().startswith("---"):
        raise ValueError(f"topic revision {field} must not contain frontmatter")
    return value


def _claim_indexes(value: Any, claim_count: int) -> list[int]:
    """Validate one non-empty, sorted-independent set of claim indexes."""
    if (not isinstance(value, list) or not value or any(type(index) is not int for index in value)
            or len(set(value)) != len(value) or any(index < 0 or index >= claim_count for index in value)):
        raise ValueError("topic revision claimIndexes are invalid")
    return sorted(value)


def _check_placeholders(body: str, indexes: list[int], claim_count: int) -> None:
    """Require each selected claim to be cited and reject unknown placeholders."""
    found = [int(value) for value in PLACEHOLDER.findall(body)]
    remainder = PLACEHOLDER.sub("", body)
    if ("{{claim:" in remainder or any(index >= claim_count for index in found)
            or set(found) != set(indexes)):
        raise ValueError("topic revision claim placeholders are invalid")


def _validate_revision_item(item: Any, claims: list[Any], covered: set[int], seen_pages: set[str]) -> dict[str, Any]:
    """Validate one revision and atomically claim its page and claim indexes."""
    required = {"pageId", "topicId", "title", "topic", "decisionObject", "basisHash", "body", "claimIndexes"}
    allowed = required | {"citationRetirements", "topicScope"}
    if not isinstance(item, dict) or not required <= set(item) <= allowed:
        raise ValueError("topic revision fields are invalid")
    if "topicScope" in item and item["topicScope"] != "semantic":
        raise ValueError("topic revision topicScope is invalid")
    page_id = _page_id(item["pageId"])
    if page_id in seen_pages:
        raise ValueError("topic revision page appears more than once")
    revision = {"pageId": page_id, "topicId": _bounded_text(item["topicId"], "topicId", 64),
                "title": _bounded_text(item["title"], "title"), "topic": _bounded_text(item["topic"], "topic"),
                "decisionObject": _bounded_text(item["decisionObject"], "decisionObject"),
                "basisHash": item["basisHash"], "body": _body(item["body"]),
                "claimIndexes": _claim_indexes(item["claimIndexes"], len(claims))}
    if "topicScope" in item:
        revision["topicScope"] = "semantic"
    if not is_hash(revision["topicId"]):
        raise ValueError("topic revision topicId is invalid")
    if revision["basisHash"] is not None and not is_hash(revision["basisHash"]):
        raise ValueError("topic revision basisHash is invalid")
    indexes = revision["claimIndexes"]
    if covered.intersection(indexes):
        raise ValueError("topic revision claim coverage overlaps")
    for index in indexes:
        claim = claims[index]
        if not isinstance(claim, dict) or claim.get("targetPageId") != page_id:
            raise ValueError("topic revision claim target does not match page")
        if claim.get("topic") != revision["topic"] or claim.get("decisionObject") != revision["decisionObject"]:
            raise ValueError("topic revision identity does not match claim")
    _check_placeholders(revision["body"], indexes, len(claims))
    if "citationRetirements" in item:
        from citation_retirement_contract import validate_citation_retirements
        revision["citationRetirements"] = validate_citation_retirements(
            item["citationRetirements"], revision["body"], indexes)
    seen_pages.add(page_id)
    covered.update(indexes)
    return revision


def validate_topic_revisions(value: Any, claims: list[Any], project_id: str | None = None) -> list[dict[str, Any]]:
    """Validate complete topic page revisions and exact claim coverage."""
    if not isinstance(claims, list) or not 1 <= len(claims) <= 5:
        raise ValueError("topic revision claims are invalid")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REVISIONS:
        raise ValueError("topicRevisions must be a non-empty array")
    covered: set[int] = set()
    seen_pages: set[str] = set()
    normalized = [_validate_revision_item(item, claims, covered, seen_pages) for item in value]
    if covered != set(range(len(claims))):
        raise ValueError("topic revision claims are not exactly covered")
    return normalized


def _previous_pages(value: Any) -> list[dict[str, str]]:
    """Validate prior page identities retained by a migration snapshot."""
    if not isinstance(value, list) or len(value) > MAX_PAGES:
        raise ValueError("topic migration previousPages are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"pageId", "sha256"}:
            raise ValueError("topic migration previousPages are invalid")
        page_id = _page_id(item["pageId"], "previousPageId")
        if page_id in seen or not is_hash(item["sha256"]):
            raise ValueError("topic migration previousPages are invalid")
        seen.add(page_id)
        result.append({"pageId": page_id, "sha256": item["sha256"]})
    return sorted(result, key=lambda item: item["pageId"])


def _validate_migration_page(item: Any, seen_pages: set[str], seen_previous: set[str]) -> dict[str, Any]:
    """Validate one migration target and its one-to-one old-page mapping."""
    required = {"projectId", "projectLabel", "pageId", "topicId", "title", "topic", "decisionObject", "body", "previousPages"}
    allowed = required | {"citationRetirements"}
    if not isinstance(item, dict) or set(item) not in (required, allowed):
        raise ValueError("topic migration page fields are invalid")
    page_id = _page_id(item["pageId"])
    if page_id in seen_pages:
        raise ValueError("topic migration page is duplicated")
    page = {"projectId": _bounded_text(item["projectId"], "projectId"),
            "projectLabel": _bounded_text(item["projectLabel"], "projectLabel"), "pageId": page_id,
            "topicId": _bounded_text(item["topicId"], "topicId", 64), "title": _bounded_text(item["title"], "title"),
            "topic": _bounded_text(item["topic"], "topic"), "decisionObject": _bounded_text(item["decisionObject"], "decisionObject"),
            "body": _body(item["body"]), "previousPages": _previous_pages(item["previousPages"])}
    if not is_hash(page["topicId"]):
        raise ValueError("topic migration topicId is invalid")
    previous_ids = {entry["pageId"] for entry in page["previousPages"]}
    if seen_previous.intersection(previous_ids):
        raise ValueError("topic migration previous page is assigned twice")
    if "citationRetirements" in item:
        from citation_retirement_contract import validate_citation_retirements
        page["citationRetirements"] = validate_citation_retirements(
            item["citationRetirements"], page["body"])
    seen_pages.add(page_id)
    seen_previous.update(previous_ids)
    return page


def validate_topic_migration(value: Any, baseline_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the reviewed full-vault migration against known records."""
    if not is_hash(baseline_id) or not isinstance(value, dict):
        raise ValueError("topic migration is invalid")
    required = {"version", "basisRecordIds", "pages"}
    allowed = required | {"retiredPages"}
    if (set(value) not in (required, allowed)
            or type(value.get("version")) is not int or value["version"] != 1):
        raise ValueError("topic migration fields are invalid")
    known = {item.get("id") for item in records if isinstance(item, dict)}
    basis = value["basisRecordIds"]
    if (not isinstance(basis, list) or len(basis) > 1024 or len(set(basis)) != len(basis)
            or any(not is_hash(item) or item not in known for item in basis)):
        raise ValueError("topic migration basisRecordIds are invalid")
    pages = value["pages"]
    if not isinstance(pages, list) or len(pages) > MAX_PAGES:
        raise ValueError("topic migration pages are invalid")
    if not pages and ("retiredPages" not in value or not isinstance(value["retiredPages"], list)
                      or not value["retiredPages"]):
        raise ValueError("topic migration pages are invalid")
    seen_pages: set[str] = set()
    seen_previous: set[str] = set()
    normalized_pages = [_validate_migration_page(item, seen_pages, seen_previous) for item in pages]
    normalized = {"version": 1, "basisRecordIds": sorted(basis),
                  "pages": sorted(normalized_pages, key=lambda item: item["pageId"])}
    if "retiredPages" in value:
        from citation_retirement_contract import validate_retired_pages
        reserved = seen_pages | seen_previous
        normalized["retiredPages"] = validate_retired_pages(value["retiredPages"], reserved)
    return normalized


def canonical_migration(value: dict[str, Any]) -> str:
    """Expose canonical migration bytes for generation identities and tests."""
    return canonical(value)


def _merge_time(value: Any) -> str:
    """Require an ISO-8601 time with an explicit offset; it becomes the merged page's updatedAt."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("topic merge mergedAt is invalid")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("topic merge mergedAt is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("topic merge mergedAt requires a timezone")
    return value


def _absorbed_records(value: Any, previous_ids: set[str], records: dict[str, dict[str, Any]]) -> list[str]:
    """Each absorbed record must exist and revise at least one of the merged pages."""
    if (not isinstance(value, list) or not value or len(value) > MAX_ABSORBED
            or len(set(value)) != len(value) or any(not is_hash(item) for item in value)):
        raise ValueError("topic merge absorbedRecordIds are invalid")
    for record_id in value:
        payload = (records.get(record_id) or {}).get("payload") or {}
        revisions = payload.get("topicRevisions") or []
        if not any(isinstance(item, dict) and item.get("pageId") in previous_ids for item in revisions):
            raise ValueError("topic merge absorbs a record that revises none of its pages")
    return sorted(value)


def _validate_merge(item: Any, records: dict[str, dict[str, Any]], reserved: set[str]) -> dict[str, Any]:
    """Validate one merge: a surviving page among at least two previous pages, none merged twice."""
    required = {"pageId", "title", "topic", "decisionObject", "body", "previousPages",
                "absorbedRecordIds", "mergedAt", "reason"}
    if not isinstance(item, dict) or set(item) not in (required, required | {"citationRetirements"}):
        raise ValueError("topic merge fields are invalid")
    previous = _previous_pages(item["previousPages"])
    previous_ids = {entry["pageId"] for entry in previous}
    page_id = _page_id(item["pageId"])
    if len(previous) < 2 or page_id not in previous_ids or reserved.intersection(previous_ids):
        raise ValueError("topic merge pages are invalid")
    merge = {"pageId": page_id, "title": _bounded_text(item["title"], "title"),
             "topic": _bounded_text(item["topic"], "topic"),
             "decisionObject": _bounded_text(item["decisionObject"], "decisionObject"),
             "body": _body(item["body"]), "previousPages": previous,
             "absorbedRecordIds": _absorbed_records(item["absorbedRecordIds"], previous_ids, records),
             "mergedAt": _merge_time(item["mergedAt"]), "reason": _bounded_text(item["reason"], "reason", 1000)}
    if "citationRetirements" in item:
        from citation_retirement_contract import validate_citation_retirements
        merge["citationRetirements"] = validate_citation_retirements(item["citationRetirements"], merge["body"])
    reserved.update(previous_ids)
    return merge


def validate_topic_merges(value: Any, records: list[dict[str, Any]],
                          migration: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Validate reviewed merges of revision-layer pages; pages owned by the legacy migration stay out."""
    if not isinstance(value, list) or not value or len(value) > MAX_MERGES:
        raise ValueError("topic merges are invalid")
    known = {item.get("id"): item for item in records if isinstance(item, dict)}
    reserved: set[str] = set()
    for page in (migration or {}).get("pages", []):
        reserved.update([page["pageId"], *(entry["pageId"] for entry in page["previousPages"])])
    reserved.update(page["pageId"] for page in (migration or {}).get("retiredPages", []))
    merges = [_validate_merge(item, known, reserved) for item in value]
    return sorted(merges, key=lambda item: item["pageId"])

