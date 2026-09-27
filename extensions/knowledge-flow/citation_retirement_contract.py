"""Wire validation for reviewed citation retirement declarations.

Retirement declarations describe why one complete citation marker leaves a
page and which citation, claim placeholder, or external reference replaces it.
This module validates only the transport shape and page-body relationship;
historical ownership and source authority remain materializer concerns.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit


CITATION_MARKER = re.compile(r"^\^\[[^\]\r\n]+\]$")
MAX_RETIREMENTS = 500
MAX_CITATION_LENGTH = 1024
MAX_REASON_LENGTH = 1000
MAX_REPLACEMENT_LENGTH = 2048
MAX_URL_LENGTH = 2048
MAX_CLAIM_INDEX = 4
PLACEHOLDER = re.compile(r"^\{\{claim:([0-4])\}\}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
PAGE_ID = re.compile(r"^concepts/[^/\\:\x00]+$")


def _citation(value: Any) -> str:
    """Require one complete, non-empty, single-line citation marker."""
    if (not isinstance(value, str) or len(value) > MAX_CITATION_LENGTH
            or not CITATION_MARKER.fullmatch(value)):
        raise ValueError("citation retirement citation is invalid")
    return value


def _reason(value: Any) -> str:
    """Require a trimmed human reason that is bounded and non-empty."""
    if not isinstance(value, str):
        raise ValueError("citation retirement reason is invalid")
    if not value or value != value.strip() or len(value) > MAX_REASON_LENGTH:
        raise ValueError("citation retirement reason is invalid")
    return value


def _valid_hostname(hostname: str | None) -> bool:
    """Accept an IP literal or an RFC-style DNS name after IDNA conversion."""
    if not hostname or len(hostname) > 253:
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_name = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_name) > 253:
        return False
    if ascii_name.endswith("."):
        ascii_name = ascii_name[:-1]
    if ascii_name.endswith("."):
        return False
    labels = ascii_name.split(".")
    return bool(labels and all(
        label and len(label) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ))


def _https_url(value: Any) -> bool:
    """Require an HTTPS URL without userinfo, whitespace, or invalid host."""
    if (not isinstance(value, str) or not value.startswith("https://") or len(value) > MAX_URL_LENGTH
            or "\\" in value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and bool(parsed.netloc) and not any(char in parsed.netloc for char in "@%")
            and _valid_hostname(hostname) and not parsed.netloc.endswith(":"))


def _replacement(value: Any, body: str, claim_indexes: set[int] | None) -> str:
    """Validate replacement syntax, page presence, and revision-only claims."""
    if (not isinstance(value, str) or not value or len(value) > MAX_REPLACEMENT_LENGTH
            or value not in body):
        raise ValueError("citation retirement replacement is invalid")
    marker = CITATION_MARKER.fullmatch(value)
    placeholder = PLACEHOLDER.fullmatch(value)
    if marker is None and placeholder is None and not _https_url(value):
        raise ValueError("citation retirement replacement is invalid")
    if placeholder is not None:
        index = int(placeholder.group(1))
        if claim_indexes is None or index not in claim_indexes or index > MAX_CLAIM_INDEX:
            raise ValueError("citation retirement replacement claim is invalid")
    return value


def _retired_page_id(value: Any) -> str:
    """Keep retired-page references within one safe concepts namespace."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 256 or not PAGE_ID.fullmatch(value)):
        raise ValueError("retired pageId is invalid")
    component = value.split("/", 1)[1]
    if (component in ("", ".", "..") or component.startswith(".") or component.endswith(".")
            or any(ord(char) < 32 or ord(char) == 127 for char in component)):
        raise ValueError("retired pageId is invalid")
    return value


def validate_retired_pages(value: Any, reserved_page_ids: set[str]) -> list[dict[str, str]]:
    """Validate optional whole-page retirements against migration page ids."""
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("retiredPages must be an array of at most 1000 items")
    if not isinstance(reserved_page_ids, set):
        raise ValueError("retired page reservations are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        required = {"projectId", "pageId", "sha256", "reason", "externalReference"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("retired page fields are invalid")
        page_id = _retired_page_id(item["pageId"])
        if page_id in seen or page_id in reserved_page_ids:
            raise ValueError("retired page overlaps migration page")
        project_id = item["projectId"]
        if (not isinstance(project_id, str) or not project_id or project_id != project_id.strip()
                or len(project_id) > 160):
            raise ValueError("retired page projectId is invalid")
        sha256 = item["sha256"]
        if not isinstance(sha256, str) or not HASH.fullmatch(sha256):
            raise ValueError("retired page sha256 is invalid")
        external = item["externalReference"]
        if not _https_url(external):
            raise ValueError("retired page externalReference is invalid")
        result.append({"projectId": project_id, "pageId": page_id, "sha256": sha256,
                       "reason": _reason(item["reason"]), "externalReference": external})
        seen.add(page_id)
    return sorted(result, key=lambda item: item["pageId"])


def validate_citation_retirements(value: Any, body: str,
                                  claim_indexes: list[int] | None = None) -> list[dict[str, str]]:
    """Validate optional page citation retirements and return canonical entries.

    ``claim_indexes`` is supplied only for topic revisions.  A missing value
    therefore forbids ``{{claim:N}}`` replacements on migration pages.
    """
    if not isinstance(body, str) or not isinstance(value, list) or len(value) > MAX_RETIREMENTS:
        raise ValueError("citationRetirements must be an array of at most 500 items")
    if (claim_indexes is not None and
            (not isinstance(claim_indexes, list) or len(set(claim_indexes)) != len(claim_indexes)
             or any(type(index) is not int or index < 0 or index > MAX_CLAIM_INDEX for index in claim_indexes))):
        raise ValueError("citation retirement claim indexes are invalid")
    allowed_claims = set(claim_indexes) if claim_indexes is not None else None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"citation", "reason", "replacement"}:
            raise ValueError("citation retirement fields are invalid")
        citation = _citation(item["citation"])
        if citation in seen or citation in body:
            raise ValueError("citation retirement citation is duplicated or retained")
        replacement = _replacement(item["replacement"], body, allowed_claims)
        normalized.append({"citation": citation, "reason": _reason(item["reason"]),
                           "replacement": replacement})
        seen.add(citation)
    return normalized
