"""Record bounded hook observations for prepared Wiki evidence and visible references.

One private diagnostic file is keyed by the session and turn, while lifecycle
context has a separate record and never enters turn counts. The Stop payload is
an event observation of its final message, not a complete native host trace.
Diagnostic files retain hashes and counts, never prompt, context, or answer text.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from common import digest, load_json, safe_text, save_json


def _diagnostic_turn_id(event: dict[str, Any]) -> str | None:
    """Lifecycle replays stay outside turn counts even if the host supplies a turn ID."""
    value = None if event.get("hook_event_name") == "SessionStart" else event.get("turn_id")
    return str(value) if value else None


def diagnostic_path(config: dict[str, Any], event: dict[str, Any], reason: str) -> Path:
    """Key turn records by session and turn; keep lifecycle observations separate."""
    session_id = str(event.get("session_id", ""))
    turn_id = _diagnostic_turn_id(event)
    key = (session_id + ":turn:" + str(turn_id) if turn_id else
           session_id + ":lifecycle:" + reason)
    return Path(config["stateDir"]) / "context-diagnostics" / (digest(key) + ".json")


def _reference_hashes(references: list[dict[str, Any]]) -> list[str]:
    """Hash source locators so maintenance data never retains citation text."""
    values = []
    for reference in references:
        encoded = json.dumps([reference["pageId"], reference["pageRevision"], reference["citations"]],
                             ensure_ascii=False, separators=(",", ":"))
        values.append(digest(encoded))
    return list(dict.fromkeys(values))


def write_diagnostic(config: dict[str, Any], event: dict[str, Any], project: str | None,
                     reason: str, result: dict[str, Any], prompt: str) -> None:
    """Upsert one private observation without retaining prompt or context bodies."""
    diagnostics = result.get("diagnostics", {}) if isinstance(result, dict) else {}
    turn_id = _diagnostic_turn_id(event)
    lifecycle = not turn_id
    references = result.get("references", []) if isinstance(result, dict) else []
    references = references if isinstance(references, list) else []
    path = diagnostic_path(config, event, reason)
    previous = _load_object(path)
    observed_at = _now()
    value = {"kind": "lifecycle" if lifecycle else "turn", "at": observed_at,
             "observedAt": observed_at, "status": result.get("status", "unknown"),
             "reason": reason, "projectId": project, "queryHash": digest(prompt),
             "prepared": bool(result.get("preparedEvidence", result.get("prepared"))),
             "preparedEvidence": bool(result.get("preparedEvidence", result.get("prepared"))),
             "preparedCount": int(result.get("preparedCount", len(references)) or 0),
             "complete": bool(result.get("complete")), "referenceHashes": _reference_hashes(references),
             "referencesTracked": bool(result.get("referencesTracked")),
             "turnHash": digest(str(turn_id)) if turn_id else None,
             "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
             "counts": {key: diagnostics[key] for key in ("scopedPages", "matchedSections")
                        if isinstance(diagnostics, dict) and key in diagnostics}}
    if isinstance(diagnostics, dict) and diagnostics.get("errorType"):
        value["errorType"] = diagnostics["errorType"]
    if isinstance(previous, dict) and previous.get("turnHash") == value["turnHash"]:
        for key in ("stopObservedAt", "stopMessageObserved", "explicitReference"):
            if key in previous:
                value[key] = previous[key]
    save_json(path, value)


def save_prepared_context(config: dict[str, Any], event: dict[str, Any], session: dict[str, Any],
                          project: str | None, prompt: str, result: dict[str, Any]) -> None:
    """Keep exact source identifiers temporarily for same-turn Stop matching."""
    prepared = {"turnId": event.get("turn_id"), "projectId": project,
                "seen": result.get("seen", {}), "text": result.get("context", ""),
                "complete": bool(result.get("complete")),
                "references": result.get("references", []),
                "referencesTracked": bool(result.get("referencesTracked"))}
    save_json(_session_path(config, event), {**session, "readProjectId": project,
                                             "readPrompt": safe_text(prompt, 12000),
                                             "preparedRead": prepared})


def promote_prepared_context(config: dict[str, Any], event: dict[str, Any]) -> None:
    """Clear this turn's temporary packet and advance only complete seen state."""
    path = _session_path(config, event)
    session = _load_object(path)
    prepared = session.get("preparedRead") if isinstance(session, dict) else None
    if not isinstance(prepared, dict) or prepared.get("turnId") != event.get("turn_id"):
        return
    seen = prepared.get("seen")
    updated = {**session, "preparedRead": None}
    if isinstance(seen, dict) and prepared.get("complete"):
        updated["seen"] = seen
    save_json(path, updated)


def record_stop_observation(config: dict[str, Any], event: dict[str, Any]) -> None:
    """Match exact prepared references in Stop's event snapshot of the final text."""
    path = diagnostic_path(config, event, "")
    value = _load_object(path)
    turn_id = event.get("turn_id")
    if (value.get("kind") != "turn"
            or value.get("turnHash") != digest(str(turn_id)) or value.get("stopObservedAt")):
        return
    message = event.get("last_assistant_message")
    observed_message = isinstance(message, str)
    references_tracked = False
    references: list[dict[str, Any]] = []
    session = _load_object(_session_path(config, event))
    prepared = session.get("preparedRead") if isinstance(session, dict) else None
    if isinstance(prepared, dict) and prepared.get("turnId") == turn_id:
        references = prepared.get("references", []) if isinstance(prepared.get("references"), list) else []
        references_tracked = bool(prepared.get("referencesTracked"))
    explicit = None
    if observed_message and references_tracked:
        final_text = safe_text(message, 16000)
        found = any(_references_match(final_text, item) for item in references if isinstance(item, dict))
        explicit = True if found else (None if final_text != message else False)
    observed_at = _now()
    value.update({"stopObservedAt": observed_at, "stopMessageObserved": observed_message,
                  "observedAt": observed_at, "explicitReference": explicit})
    save_json(path, value)


def _references_match(text: str, reference: dict[str, Any]) -> bool:
    """Require an exact citation marker or a page ID with path boundaries."""
    citations = reference.get("citations", [])
    if isinstance(citations, list) and any(isinstance(item, str) and item and item in text for item in citations):
        return True
    page_id = reference.get("pageId")
    if not isinstance(page_id, str) or not page_id:
        return False
    pattern = r"(?<![\w./-])" + re.escape(page_id) + r"(?![\w./-])"
    return re.search(pattern, text) is not None


def _session_path(config: dict[str, Any], event: dict[str, Any]) -> Path:
    """Resolve the private per-session cursor file."""
    return Path(config["stateDir"]) / "sessions" / (digest(str(event.get("session_id", ""))) + ".json")


def _load_object(path: Path) -> dict[str, Any]:
    """Treat a malformed diagnostic as absent so observation cannot block a turn."""
    try:
        value = load_json(path, {})
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _now() -> str:
    """Return a timezone-aware UTC observation timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


__all__ = ["diagnostic_path", "promote_prepared_context", "record_stop_observation",
           "save_prepared_context", "write_diagnostic"]
