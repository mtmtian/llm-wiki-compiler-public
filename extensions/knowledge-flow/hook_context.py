"""Bounded read delivery for native hooks; write intake remains in ``hooks``."""

from __future__ import annotations

import re
from typing import Any, Callable

from common import safe_text


MAX_HOOK_CHARS = 6000
DEFAULT_HOOK_CHARS = 2400
MAX_OPERATIONAL_CHARS = 1000
OPERATIONAL_PREFIX = "Wiki当前运维状态\n"
MAX_REFERENCES = 128
MAX_REFERENCE_TEXT = 512
CLAIM_REFERENCE = re.compile(r"[0-9a-f]{64}:(?:0|[1-9]\d*)")
RECORD_REVISION = re.compile(r"[0-9a-f]{64}")


def _limit(config: dict[str, Any]) -> int:
    """Clamp the adapter's output to a bounded character budget."""
    try:
        configured = int(config.get("maxContextChars", DEFAULT_HOOK_CHARS))
    except (TypeError, ValueError):
        configured = DEFAULT_HOOK_CHARS
    return max(0, min(MAX_HOOK_CHARS, configured))


def _failure(project: str, error: Exception, limit: int, operation: str) -> dict[str, Any]:
    """Return a short actionable degradation without pretending retrieval worked."""
    hint = f"Wiki检索暂时不可用（项目：{project}）。请用 MCP get_project_context/read_page 补查当前主题。"
    operation = operation[:max(0, limit - len(hint))]
    text = (operation + "\n" + hint)[:limit] if operation else hint[:limit]
    return {"context": text[:limit], "seen": {}, "prepared": False, "preparedCount": 0,
            "references": [], "referencesTracked": False,
            "status": "degraded", "complete": False,
            "diagnostics": {"errorType": type(error).__name__}}


def _references(value: Any) -> list[dict[str, Any]]:
    """Keep bounded legacy page and immutable ledger-claim references."""
    if not isinstance(value, list) or len(value) > MAX_REFERENCES:
        return []
    result = []
    for item in value[:MAX_REFERENCES]:
        if not isinstance(item, dict):
            continue
        page_id, revision = item.get("pageId"), item.get("pageRevision")
        claim_ref, record_revision = item.get("claimRef"), item.get("recordRevision")
        citations = item.get("citations")
        if isinstance(page_id, str) and page_id and len(page_id) <= MAX_REFERENCE_TEXT \
                and isinstance(revision, str) and len(revision) <= MAX_REFERENCE_TEXT:
            identity = {"pageId": page_id, "pageRevision": revision}
        elif (isinstance(claim_ref, str) and CLAIM_REFERENCE.fullmatch(claim_ref)
              and isinstance(record_revision, str) and RECORD_REVISION.fullmatch(record_revision)):
            identity = {"claimRef": claim_ref, "recordRevision": record_revision}
        else:
            continue
        markers = [marker for marker in citations if isinstance(marker, str) and marker
                   and len(marker) <= MAX_REFERENCE_TEXT] if isinstance(citations, list) else []
        result.append({**identity, "citations": list(dict.fromkeys(markers))})
    return result


def _prepared_result(result: dict[str, Any], operation: str, business: str,
                     limit: int, clipped: bool) -> dict[str, Any]:
    """Bind rendered sources to the text that survived the local output budget."""
    visible_complete = not clipped
    complete = bool(result.get("complete", True)) and visible_complete
    candidate = result.get("seen", {}) if complete and isinstance(result.get("seen", {}), dict) else {}
    raw_references = result.get("references")
    has_reference_list = isinstance(raw_references, list)
    references = _references(raw_references) if visible_complete and business and has_reference_list else []
    reference_count = len(references)
    tracked = has_reference_list and len(references) == len(raw_references)
    prepared = visible_complete and bool(business) and reference_count > 0
    diagnostics = result.get("diagnostics", {})
    return {"context": (operation + business)[:limit], "seen": candidate, "prepared": prepared,
            "preparedCount": reference_count, "references": references,
            "referencesTracked": tracked, "status": result.get("status", "no-hit"),
            "complete": complete,
            "diagnostics": diagnostics if isinstance(diagnostics, dict) else {}}


def prepare_context(config: dict[str, Any], project: str, prompt: str,
                    allowed: list[str], seen: dict[str, str], invoke: Callable[..., dict[str, Any]],
                    operational: str = "") -> dict[str, Any]:
    """Run scoped retrieval and return prepared text plus an uncommitted seen cache."""
    limit = _limit(config)
    operation = safe_text(operational, MAX_OPERATIONAL_CHARS)
    prefix = OPERATIONAL_PREFIX if operation else ""
    operation = (prefix + operation + ("\n" if operation else ""))[:limit]
    budget = max(0, limit - len(operation))
    if not project:
        return {"context": operation, "seen": {}, "prepared": False, "preparedCount": 0,
                "references": [], "referencesTracked": False,
                "status": "no-scope", "complete": bool(operation), "diagnostics": {}}
    if not budget:
        return {"context": operation, "seen": {}, "prepared": False, "preparedCount": 0,
                "references": [], "referencesTracked": False,
                "status": "no-hit", "complete": False, "diagnostics": {"budget": 0}}
    request_config = {**config, "maxContextChars": budget}
    payload = {"projectId": project, "prompt": safe_text(prompt, 12000),
               "allowedPageIds": allowed, "seen": seen}
    try:
        result = invoke(request_config, "context", payload, 4)
    except Exception as error:  # worker failures must not block a user turn
        return _failure(project, error, limit, operation)
    business = safe_text(result.get("context", ""), budget)
    clipped = str(result.get("context", "")) != business
    return _prepared_result(result, operation, business, limit, clipped)


__all__ = ["prepare_context"]
