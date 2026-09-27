"""Validate reviewed whole-page replacements and classify replay conflicts.

The receipt only changes how an existing single-record conflict is reported.
It never changes publication records, materialization inputs, or visibility.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from common import _block_projects, _header_scalar
from shared_files import SharedFiles
from publication_resolution_contract import available_resolutions, index_records, target_page, validate_receipt

RECEIPT_PATH = "v2/publication-resolutions.json"
MAX_RECEIPT_BYTES = 256 * 1024
MAX_GENERATED_TEXT_BYTES = 256 * 1024
MAX_FRONTMATTER_CHARS = 16_000

def load_resolutions(config: dict[str, Any], baseline_id: str,
                     records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Load and strictly validate the optional shared resolution receipt."""
    exchange = config.get("exchange") if isinstance(config, dict) else None
    root = exchange.get("root") if isinstance(exchange, dict) else None
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("exchange.root must be an absolute path")
    exchange_root = Path(root)
    if not exchange_root.exists() and not exchange_root.is_symlink():
        return []
    with SharedFiles(exchange_root) as files:
        encoded = files.read(RECEIPT_PATH, max_bytes=MAX_RECEIPT_BYTES)
    if encoded is None:
        return []
    return validate_receipt(encoded, baseline_id, records)


def classify_conflicts(resolutions: list[dict[str, Any]], status: dict[str, Any],
                       records: list[dict[str, Any]]) -> dict[str, Any]:
    """Copy status and move only proven single-record conflicts to resolved."""
    result = copy.deepcopy(status)
    if not resolutions:
        return result
    if not isinstance(result, dict):
        raise ValueError("replica status must be an object")
    indexed = index_records(records)
    normalized = available_resolutions(resolutions, indexed, result.get("baselineId"))
    conflicts, visible, generation = (result.get("conflicts"),
                                      result.get("fullyVisibleRecordIds"), result.get("generationRoot"))
    if not isinstance(conflicts, list) or not isinstance(visible, list) or not isinstance(generation, str):
        return result
    visible_ids = {item for item in visible if isinstance(item, str)}
    raw_conflicted_ids = {identifier for conflict in conflicts
                          if isinstance(conflict, dict) and isinstance(conflict.get("recordIds"), list)
                          for identifier in conflict["recordIds"] if isinstance(identifier, str)}
    pending: list[Any] = []
    resolved: list[dict[str, Any]] = []
    checked: dict[str, bool] = {}
    for conflict in conflicts:
        resolution = _matching_resolution(conflict, normalized, visible_ids, raw_conflicted_ids)
        if resolution is None:
            pending.append(conflict)
            continue
        old_id = resolution["recordId"]
        if old_id not in checked:
            checked[old_id] = _replacement_is_visible(generation, resolution, indexed)
        if not checked[old_id]:
            pending.append(conflict)
            continue
        entry = {**copy.deepcopy(conflict), **{key: value for key, value in resolution.items() if key != "reason"}}
        entry["resolutionReason"] = resolution["reason"]
        resolved.append(entry)
    if resolved:
        result["conflicts"] = pending
        result["resolvedConflicts"] = [*result.get("resolvedConflicts", []), *resolved]
    return result


def _matching_resolution(conflict: Any, resolutions: dict[str, dict[str, Any]],
                         visible_ids: set[str], raw_conflicted_ids: set[str]) -> dict[str, Any] | None:
    """Select only a conflict whose sole record is visibly replaced."""
    record_ids = conflict.get("recordIds") if isinstance(conflict, dict) else None
    if not isinstance(record_ids, list) or len(record_ids) != 1:
        return None
    resolution = resolutions.get(record_ids[0])
    if (resolution is None or resolution["coveredBy"] not in visible_ids
            or resolution["coveredBy"] in raw_conflicted_ids):
        return None
    return resolution


def _replacement_is_visible(generation: str, resolution: dict[str, Any],
                            records: dict[str, dict[str, Any]]) -> bool:
    """Check current page provenance and its exact generated source citations."""
    if not Path(generation).is_absolute():
        return False
    replacement = records.get(resolution["coveredBy"])
    if replacement is None:
        return False
    try:
        with SharedFiles(Path(generation)) as files:
            return _mapped_claims_visible(files, replacement, resolution["claimMappings"])
    except (OSError, ValueError, UnicodeError):
        return False


def _mapped_claims_visible(files: SharedFiles, record: dict[str, Any],
                           mappings: list[dict[str, Any]]) -> bool:
    """Verify each mapped ref, page citation, and source quote segment."""
    payload = record["payload"]
    source_name = _source_name(payload, record["id"])
    source = _read_text(files, "sources/" + source_name)
    if source is None:
        return False
    pages: dict[str, tuple[str, str]] = {}
    for mapping in mappings:
        for index in mapping["coveredByIndexes"]:
            claim = payload["claims"][index]
            target = target_page(claim)
            if target not in pages:
                page = _read_text(files, "wiki/" + target + ".md")
                parsed = _page_parts(page) if page is not None else None
                if parsed is None or not _page_owned(parsed[0], payload["projectId"]):
                    return False
                pages[target] = parsed
            if not _has_live_citation(*pages[target], source_name, source,
                                      f"{record['id']}:{index}", claim, index):
                return False
    return True


def _read_text(files: SharedFiles, relative: str) -> str | None:
    """Read one generated text file through the bounded shared-file guard."""
    encoded = files.read(relative, max_bytes=MAX_GENERATED_TEXT_BYTES)
    if encoded is None:
        return None
    return encoded.decode("utf-8")


def _page_parts(text: str) -> tuple[str, str] | None:
    """Extract a bounded generated page frontmatter block and body."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    match = re.match(r"\A---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|\Z)([\s\S]*)\Z", text)
    if match is None or len(match.group(1)) > MAX_FRONTMATTER_CHARS:
        return None
    return match.group(1), match.group(2)


def _page_owned(header: str, project_id: str) -> bool:
    """Confirm project ownership for legacy and semantic generated pages."""
    if _header_scalar(header, "topicScope") == "semantic":
        return project_id in _frontmatter_list(header, "sourceProjectIds")
    return _header_scalar(header, "projectId") == project_id


def _frontmatter_list(header: str, key: str) -> list[str]:
    """Read the simple string-list form emitted by the compiler serializer."""
    match = re.search(r"^" + re.escape(key) + r"[ \t]*:[ \t]*(.*?)(?:\r?\n|$)", header, re.MULTILINE)
    if match is None:
        return []
    inline = match.group(1).strip()
    try:
        values = json.loads(inline) if inline else _block_projects(header[match.end():])
    except (ValueError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) and all(isinstance(item, str) for item in values) else []


def _has_live_citation(header: str, body: str, source_name: str, source: str,
                       reference: str, claim: dict[str, Any], index: int) -> bool:
    """Require active provenance and citations for every quote in the claim."""
    if reference not in _frontmatter_list(header, "knowledgePublicationRefs"):
        return False
    if source_name not in _frontmatter_list(header, "sources"):
        return False
    citations = _claim_citations(source, source_name, claim, index)
    return citations is not None and all(citation in body for citation in citations)


def _claim_citations(source: str, source_name: str, claim: dict[str, Any], index: int) -> list[str] | None:
    """Validate a source section and return citations for its main and support quotes."""
    lines = source.splitlines()
    heading = f"## {index + 1}. {claim.get('title')}"
    heading_indexes = [position for position, line in enumerate(lines) if line == heading]
    if len(heading_indexes) != 1:
        return None
    citations: list[str] = []
    cursor = heading_indexes[0] + 2
    quote = claim.get("quote")
    if not isinstance(quote, str) or not _quote_at(lines, cursor, quote):
        return None
    citations.append(_citation(source_name, cursor + 1, len(quote.split("\n"))))
    cursor += len(quote.split("\n")) + 6
    supports = claim.get("supportingQuotes", [])
    if not isinstance(supports, list):
        return None
    for item in supports:
        support = item.get("quote") if isinstance(item, dict) else None
        if lines[cursor:cursor + 1] != ["支持依据："] or not isinstance(support, str):
            return None
        cursor += 1
        if not _quote_at(lines, cursor, support):
            return None
        citations.append(_citation(source_name, cursor + 1, len(support.split("\n"))))
        cursor += len(support.split("\n")) + 6
    return citations


def _quote_at(lines: list[str], start: int, quote: str) -> bool:
    """Match exact quote lines at one source position."""
    quote_lines = quote.split("\n")
    return bool(quote_lines) and lines[start:start + len(quote_lines)] == quote_lines


def _citation(source_name: str, start: int, line_count: int) -> str:
    """Format one source line range as the compiler citation marker."""
    end = start + line_count - 1
    span = str(start) if end == start else f"{start}-{end}"
    return f"^[{source_name}:{span}]"


def _source_name(payload: dict[str, Any], record_id: str) -> str:
    """Mirror the materializer's stable source bundle filename."""
    identity = payload.get("repoIdentity")
    if (payload.get("projectLabel") == payload.get("projectId") and isinstance(identity, str)
            and re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", identity)):
        raw = identity.rsplit("/", 1)[-1]
    else:
        raw = str(payload.get("projectLabel", ""))
    normalized = unicodedata.normalize("NFC", raw)
    readable = re.sub(r"-+", "-", re.sub(r"[^\w]|_", "-", normalized, flags=re.UNICODE)).strip("-")[:64]
    return f"{readable or 'project'}-{str(payload.get('createdAt', ''))[:10]}-{record_id[:12]}.md"
