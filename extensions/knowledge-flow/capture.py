"""Fail-closed visible conversation evidence capture.

Native transcript parsing is isolated in :mod:`capture_transcript`.  This
module applies evidence policy, scoped prior context, and the host fallback;
named transcript failures remain explicit so intake can retry them later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import digest, load_json, safe_text
from capture_transcript import (MAX_NAMED_SCAN_BYTES, MAX_TRANSCRIPT_BYTES,
                                allowed_transcript, current_messages, read_lines_result,
                                resolve_transcript, validated_context, message)

MAX_ITEM_CHARS = 120_000
MAX_PRIOR_ITEMS = 2
CONFIRMATION = re.compile(
    r"^(?:继续|可以|好的?|行|按这个|照这个|就这样|修一下|再试|确认|同意|continue|yes|ok)(?:[，。！!、\s].*)?$",
    re.IGNORECASE,
)
EMPTY_ACKS = re.compile(r"^(?:嗯+|哦+|好+|收到|了解|谢谢|thx|thanks|ok(?:ay)?)[。！!,.，\s]*$", re.I)
ACK_INSTRUCTION = re.compile(
    r"^(?:请)?(?:只)?(?:回复|回答|说|输出)[：:\s]*(?:好(?:的)?|收到|了解|ok(?:ay)?)[。！!,.，\s]*$", re.I)
EXPLICIT_CONFIRMATION = re.compile(r"^(?:确认|同意|按这个|照这个|就这样|yes)(?:[，。！!、\s].*)?$", re.I)


def _item(role: str, text: str, identifier: str, locator: str, observed: str,
          current: bool = True, historical: bool = False,
          status: str = "observed") -> dict[str, Any]:
    """Create the stable, redacted evidence shape shared by both sources."""
    value = safe_text(text, MAX_ITEM_CHARS).strip()
    return {"id": "codex-" + digest(identifier)[:24], "kind": role, "text": value,
            "locator": locator, "sha256": digest(value), "observedAt": observed,
            "current": current, "historical": historical, "evidenceStatus": status}


def _message(payload, event, observed, locator_prefix, current=True, historical=False):
    """Keep the old private adapter while transcript parsing lives elsewhere."""
    return message(payload, event, observed, locator_prefix, _item, MAX_ITEM_CHARS,
                   current=current, historical=historical)


def _allowed_transcript(path: str, config: dict[str, Any]) -> Path | None:
    """Return a named transcript only if it stays under an approved root."""
    return allowed_transcript(path, config)


def _codex_evidence_result(event: dict[str, Any], record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Read one verified named rollout and return a precise failure reason."""
    raw_path = event.get("transcript_path")
    if not isinstance(raw_path, str):
        return {"evidence": [], "reason": "transcript-path-missing"}
    path, path_reason = resolve_transcript(raw_path, config)
    if path is None:
        return {"evidence": [], "reason": path_reason}
    if not event.get("session_id") or not event.get("turn_id"):
        return {"evidence": [], "reason": "event-identity-missing"}
    records, read_reason = read_lines_result(path)
    if records is None:
        return {"evidence": [], "reason": read_reason}
    context = validated_context(records, event)
    if context is None:
        return {"evidence": [], "reason": "transcript-session-or-cwd-mismatch"}
    context_ordinal, _ = context
    current, has_completion = current_messages(records, context_ordinal, event, record,
                                               _item, MAX_ITEM_CHARS)
    if not has_completion and event.get("hook_event_name") == "Stop":
        expected = safe_text(event.get("last_assistant_message"), MAX_ITEM_CHARS).strip()
        has_completion = bool(expected and any(item["kind"] == "assistant" and item["text"] == expected
                                               for item in current))
    if not has_completion:
        return {"evidence": [], "reason": "transcript-completion-missing"}
    return {"evidence": _with_prior_context(current, records, context_ordinal, event, record, config),
            "reason": "", "sourcePath": str(path)}


def _codex_evidence(event: dict[str, Any], record: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only verified native evidence."""
    return _codex_evidence_result(event, record, config)["evidence"]


def _with_prior_context(current, records, context_ordinal, event, record, config):
    """Include at most one prior visible exchange for an explicitly routed ack."""
    if not current or not _is_confirmation(str(event.get("prompt", ""))):
        return current
    allowed = bool(event.get("routing_evidence")) or record.get("reason") in {
        "business-continuation", "business-workspace", "owned-repository"}
    if not allowed:
        return current
    prior_turns = []
    for entry in records:
        if entry.get("ordinal", -1) >= context_ordinal or entry.get("type") != "response_item":
            continue
        payload = entry.get("payload")
        metadata = payload.get("internal_chat_message_metadata_passthrough") if isinstance(payload, dict) else None
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if turn_id and turn_id != event.get("turn_id") and turn_id not in prior_turns:
            prior_turns.append(turn_id)
    if not prior_turns or not _prior_turn_matches_project(prior_turns[-1], event, record, config):
        return current
    previous_turn, prior = prior_turns[-1], []
    for entry in records:
        if entry.get("ordinal", -1) >= context_ordinal or entry.get("type") != "response_item":
            continue
        payload = entry.get("payload")
        metadata = payload.get("internal_chat_message_metadata_passthrough") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict) or metadata.get("turn_id") != previous_turn:
            continue
        item = _message(payload, {**event, "turn_id": previous_turn},
                        str(entry.get("timestamp") or record.get("createdAt", "")),
                        "codex://" + str(event["session_id"]) + "/turn/" + str(previous_turn),
                        current=False, historical=True)
        if item is not None:
            prior.append(item)
    return prior[-MAX_PRIOR_ITEMS:] + current


def _prior_turn_matches_project(turn_id, event, record, config):
    """Require a local routed-turn record to bind nearby context to this project."""
    if not isinstance(config.get("stateDir"), str):
        return False
    path = Path(config["stateDir"]) / "turns" / (digest(str(event.get("session_id", "")) + ":" + turn_id) + ".json")
    try:
        previous = load_json(path, {})
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(previous, dict) and previous.get("projectId") == record.get("projectId")


def _is_confirmation(text: str) -> bool:
    """Recognize a short acknowledgement that needs nearby routed context."""
    return bool(CONFIRMATION.fullmatch(text.strip()) or EMPTY_ACKS.fullmatch(text.strip()))


def _host_evidence(event: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Trust only host supplied visible user/assistant messages marked complete."""
    supplied = event.get("conversation_evidence")
    if not isinstance(supplied, list):
        return []
    result = []
    for index, item in enumerate(supplied):
        if not isinstance(item, dict) or item.get("kind") not in ("user", "assistant"):
            continue
        if item.get("truncated") is True or item.get("complete") is False:
            return []
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_ITEM_CHARS:
            continue
        identifier = str(item.get("id") or f"host-{index}-{digest(text)[:12]}")
        locator = str(item.get("locator") or "codex://" + str(event.get("session_id", "")) + "/item/" + identifier)
        status = "assistant-claim" if item["kind"] == "assistant" else "observed"
        result.append(_item(item["kind"], text, identifier, locator,
                            str(item.get("observedAt") or item.get("timestamp") or record.get("createdAt", ""))[:80],
                            status=status))
    return result


def capture_evidence_result(event: dict[str, Any], record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return evidence and precise status without synthetic named-transcript fallback."""
    native = _codex_evidence_result(event, record, config) if isinstance(event.get("transcript_path"), str) else None
    if native and native["evidence"]:
        return {"evidence": native["evidence"], "status": "ok", "source": "transcript",
                "sourcePath": native.get("sourcePath")}
    host = _host_evidence(event, record)
    if host:
        return {"evidence": host, "status": "ok", "source": "host"}
    if isinstance(event.get("transcript_path"), str):
        return {"evidence": [], "status": "unavailable",
                "reason": (native or {}).get("reason", "named-transcript-unavailable")}
    prompt = safe_text(record.get("prompt") or event.get("prompt"), 12000).strip()
    if not prompt:
        return {"evidence": [], "status": "empty", "reason": "no-visible-user-prompt"}
    result = [_item("user", prompt, "fallback-user-" + str(event.get("turn_id", "")),
                    "codex://" + str(event.get("session_id", "")) + "/turn/" + str(event.get("turn_id", "")),
                    str(record.get("createdAt", "")), status="current-user")]
    assistant = safe_text(event.get("last_assistant_message"), 16000).strip()
    if assistant:
        result.append(_item("assistant", assistant, "fallback-assistant-" + str(event.get("turn_id", "")),
                            "codex://" + str(event.get("session_id", "")) + "/turn/" + str(event.get("turn_id", "")) + "/assistant",
                            str(record.get("createdAt", "")), status="assistant-claim"))
    return {"evidence": result, "status": "ok", "source": "hook-fallback"}


def capture_evidence(event, record, config):
    """Capture visible evidence while preserving the original list-only API."""
    return capture_evidence_result(event, record, config)["evidence"]


def has_substantive_evidence(evidence):
    """Return true for a user contribution or a routed confirmation with substance."""
    if not isinstance(evidence, list):
        return False
    users = [item for item in evidence if isinstance(item, dict) and item.get("kind") == "user"
             and item.get("current") is not False and not item.get("historical")]
    if not users or not str(users[-1].get("text", "")).strip():
        return False
    text = str(users[-1]["text"]).strip()
    if not _is_confirmation(text) and not ACK_INSTRUCTION.fullmatch(text):
        return True
    if EXPLICIT_CONFIRMATION.fullmatch(text) and any(item.get("historical") for item in evidence):
        return True
    return any(_substantive_assistant(item) for item in evidence)


def _substantive_assistant(item):
    """Treat a current assistant explanation as a candidate, never execution proof."""
    if item.get("kind") != "assistant" or item.get("historical") or item.get("current") is False:
        return False
    text = str(item.get("text", "")).strip()
    return len(text) >= 8 and not EMPTY_ACKS.fullmatch(text) and not re.fullmatch(
        r"(?:已完成|完成|收到|好的?|可以|确认|同意|明白|了解|ok|okay)[。！!,.，\s]*", text, re.I)


__all__ = ["MAX_ITEM_CHARS", "MAX_NAMED_SCAN_BYTES", "MAX_TRANSCRIPT_BYTES",
           "capture_evidence", "capture_evidence_result", "has_substantive_evidence"]
