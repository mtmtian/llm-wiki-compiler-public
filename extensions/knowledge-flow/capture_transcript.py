"""Bounded, identity-checked native transcript reading.

The reader accepts only a hook-named rollout below an explicitly configured
session root.  It can resolve the exact same filename in the configured
archive root after a session is moved. Large files are scanned with explicit
I/O, time and retained-evidence bounds; bulky tool output is validated then
discarded without discarding turn identity. No session directory discovery or
fuzzy filename matching is performed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from time import monotonic
from typing import Any, Callable

MAX_TRANSCRIPT_BYTES = 8_000_000
# The named rollout's retained records exclude bulky, non-evidence tool output.
MAX_NAMED_SCAN_BYTES = 32_000_000
MAX_LINES = 20_000
MAX_CAPTURE_SCAN_BYTES = 512_000_000
MAX_CAPTURE_SCAN_SECONDS = 4
MAX_CAPTURE_LINE_BYTES = 32_000_000
VISIBLE_TEXT_TYPES = {"input_text", "output_text", "text"}


def _under(path: Path, root: Path) -> bool:
    """Check a resolved path without allowing a similarly named sibling."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def session_roots(config: dict[str, Any]) -> list[Path]:
    """Resolve only explicit roots supplied by config or ``CODEX_HOME``."""
    roots: list[Path] = []
    declared = config.get("sessionRoots")
    if isinstance(declared, list):
        roots.extend(Path(value) for value in declared if isinstance(value, str) and value)
    value = config.get("sessionsRoot")
    if isinstance(value, str) and value:
        roots.append(Path(value))
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    code_home = env.get("CODEX_HOME") or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    code_home = Path(code_home) if isinstance(code_home, str) else code_home
    if isinstance(code_home, Path):
        roots.extend([code_home / "sessions", code_home / "archived_sessions"])
    result: list[Path] = []
    for root in roots:
        if root.is_absolute():
            resolved = root.expanduser().resolve()
            if resolved not in result:
                result.append(resolved)
    return result


def resolve_transcript(path: str, config: dict[str, Any]) -> tuple[Path | None, str]:
    """Resolve a named rollout, including one exact archived filename."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return None, "transcript-path-not-absolute"
    roots = session_roots(config)
    try:
        declared = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, "transcript-path-invalid"
    if not any(_under(declared, root) for root in roots):
        return None, "transcript-outside-session-root"
    if declared.is_file():
        return declared, ""
    unique = _archive_candidates(declared, roots)
    if len(unique) == 1:
        return unique[0], "archived-exact-filename"
    if len(unique) > 1:
        return None, "archived-filename-ambiguous"
    return None, "named-transcript-missing"


def _archive_candidates(declared: Path, roots: list[Path]) -> list[Path]:
    """Check only native flat or date-preserving archive locations, never glob history."""
    archive_candidates: list[Path] = []
    for root in roots:
        if root.name != "sessions" or not _under(declared, root):
            continue
        archive = root.parent / "archived_sessions"
        try:
            archive_root = archive.resolve()
        except (OSError, RuntimeError):
            continue
        if not archive.is_dir() or not any(item == archive_root for item in roots):
            continue
        for exact in (archive / declared.relative_to(root), archive / declared.name):
            if exact.is_file():
                resolved = exact.resolve()
                if _under(resolved, archive_root):
                    archive_candidates.append(resolved)
    return list(dict.fromkeys(archive_candidates))


def allowed_transcript(path: str, config: dict[str, Any]) -> Path | None:
    """Return a verified named transcript while preserving the old helper API."""
    resolved, _ = resolve_transcript(path, config)
    return resolved


def parse_lines(raw: bytes, max_line_bytes=MAX_TRANSCRIPT_BYTES) -> tuple[list[dict[str, Any]] | None, str]:
    """Parse bounded JSONL records and explain the first rejection."""
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        return None, "transcript-invalid-utf8"
    if len(lines) > MAX_LINES:
        return None, "transcript-too-many-lines"
    records: list[dict[str, Any]] = []
    previous_ordinal = -1
    for line in lines:
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > max_line_bytes:
            return None, "transcript-line-too-large"
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None, "transcript-invalid-jsonl"
        if not isinstance(value, dict) or not isinstance(value.get("ordinal"), int):
            return None, "transcript-record-invalid"
        payload = value.get("payload")
        if value.get("truncated") is True or value.get("complete") is False:
            return None, "transcript-incomplete"
        if isinstance(payload, dict) and (payload.get("truncated") is True
                                          or payload.get("complete") is False
                                          or payload.get("sourceTextTruncated") is True):
            return None, "transcript-incomplete"
        if value["ordinal"] <= previous_ordinal:
            return None, "transcript-ordinal-invalid"
        previous_ordinal = value["ordinal"]
        records.append(value)
    return records, ""


def read_lines_result(path: Path) -> tuple[list[dict[str, Any]] | None, str]:
    """Scan long rollouts without losing turn starts behind large tool outputs."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            return _scan_capture_records(stream, size)
    except OSError:
        return None, "transcript-unreadable"


def _scan_capture_records(stream, size):
    """Bound I/O, retained evidence and time while validating every scanned record."""
    if size > MAX_CAPTURE_SCAN_BYTES:
        return None, "transcript-scan-limit-exceeded"
    deadline = monotonic() + MAX_CAPTURE_SCAN_SECONDS
    records, retained_bytes, previous_ordinal = [], 0, -1
    while stream.tell() < size:
        if monotonic() >= deadline:
            return None, "transcript-scan-timeout"
        raw = stream.readline(min(MAX_CAPTURE_LINE_BYTES + 1, size - stream.tell()))
        if not raw:
            return None, "transcript-changed-during-read"
        values, reason = parse_lines(raw, MAX_CAPTURE_LINE_BYTES)
        if values is None:
            return None, reason
        if not values:
            continue
        record = values[0]
        if record["ordinal"] <= previous_ordinal:
            return None, "transcript-ordinal-invalid"
        previous_ordinal = record["ordinal"]
        if not _capture_record(record):
            continue
        if len(raw.rstrip(b"\r\n")) > MAX_TRANSCRIPT_BYTES:
            return None, "transcript-line-too-large"
        retained_bytes += len(raw)
        if len(records) >= MAX_LINES or retained_bytes > MAX_NAMED_SCAN_BYTES:
            return None, "transcript-evidence-limit-exceeded"
        records.append(record)
    return records, ""


def _capture_record(record):
    """Keep identity, completion and visible messages, including prior confirmations."""
    kind, payload = record.get("type"), record.get("payload")
    if kind in ("session_meta", "turn_context"):
        return True
    if not isinstance(payload, dict):
        return False
    if kind == "event_msg":
        return payload.get("type") == "task_complete"
    if kind != "response_item" or payload.get("type") != "message":
        return False
    if payload.get("role") == "user":
        return True
    return (payload.get("role") == "assistant"
            and payload.get("phase") in (None, "commentary", "final_answer")
            and payload.get("channel") in (None, "commentary", "final"))


def read_lines(path: Path) -> list[dict[str, Any]] | None:
    """Compatibility wrapper returning only parsed records."""
    return read_lines_result(path)[0]


def same_cwd(value: Any, event_cwd: Any) -> bool:
    """Compare native and hook cwd after resolving symlinks."""
    if not isinstance(value, str) or not isinstance(event_cwd, str):
        return False
    try:
        return Path(value).expanduser().resolve() == Path(event_cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        return False


def validated_context(records: list[dict[str, Any]], event: dict[str, Any]) -> tuple[int, str] | None:
    """Validate session identity and the current turn workspace before its text."""
    session_ok = False
    contexts: list[tuple[int, str]] = []
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            session_ok = (payload.get("session_id") or payload.get("id")) == event.get("session_id")
        if (record.get("type") == "turn_context" and payload.get("turn_id") == event.get("turn_id")
                and same_cwd(payload.get("cwd"), event.get("cwd"))):
            contexts.append((record["ordinal"], str(payload["turn_id"])))
    return min(contexts, key=lambda item: item[0]) if session_ok and contexts else None


def text_parts(content: Any) -> str:
    """Join visible text parts while ignoring tool and metadata payloads."""
    if not isinstance(content, list):
        return ""
    values = [part.get("text", "") for part in content
              if isinstance(part, dict) and part.get("type") in VISIBLE_TEXT_TYPES
              and isinstance(part.get("text"), str)]
    return "\n".join(value for value in values if value).strip()


def message(payload: dict[str, Any], event: dict[str, Any], observed: str,
            locator_prefix: str, item_factory: Callable, max_chars: int,
            current: bool = True, historical: bool = False) -> dict[str, Any] | None:
    """Convert one visible native message through the caller's redaction factory."""
    if payload.get("type") != "message" or payload.get("role") not in ("user", "assistant"):
        return None
    if payload.get("role") == "assistant" and (
            payload.get("phase") not in (None, "commentary", "final_answer")
            or payload.get("channel") not in (None, "commentary", "final")):
        return None
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict) or metadata.get("turn_id") != event.get("turn_id"):
        return None
    text = text_parts(payload.get("content"))
    if not text or len(text) > max_chars:
        return None
    identifier = str(payload.get("id") or f"native-{observed}-{text[:16]}")
    status = "historical-context" if historical else ("assistant-claim" if payload["role"] == "assistant" else "observed")
    return item_factory(payload["role"], text, identifier, locator_prefix + "/" + identifier,
                        observed, current=current, historical=historical, status=status)


def current_messages(records: list[dict[str, Any]], context_ordinal: int,
                     event: dict[str, Any], record: dict[str, Any], item_factory: Callable,
                     max_chars: int) -> tuple[list[dict[str, Any]], bool]:
    """Collect current visible messages and verify completion ordering."""
    current: list[dict[str, Any]] = []
    completion_ordinal = -1
    last_message_ordinal = context_ordinal
    locator = "codex://" + str(event["session_id"]) + "/turn/" + str(event["turn_id"])
    for entry in records:
        payload = entry.get("payload")
        if not isinstance(payload, dict) or entry["ordinal"] <= context_ordinal:
            continue
        if entry.get("type") == "response_item":
            item = message(payload, event, str(entry.get("timestamp") or record.get("createdAt", "")),
                           locator, item_factory, max_chars)
            if item is not None:
                current.append(item)
                last_message_ordinal = entry["ordinal"]
        if (entry.get("type") == "event_msg" and payload.get("type") == "task_complete"
                and payload.get("turn_id") == event.get("turn_id")):
            completion_ordinal = entry["ordinal"]
    return current, completion_ordinal > last_message_ordinal


__all__ = ["MAX_TRANSCRIPT_BYTES", "MAX_NAMED_SCAN_BYTES", "allowed_transcript",
           "current_messages", "read_lines", "read_lines_result", "resolve_transcript",
           "same_cwd", "session_roots", "validated_context"]
