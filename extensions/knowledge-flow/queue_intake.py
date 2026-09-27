"""Validate and quarantine private queue sources before batch selection."""

from __future__ import annotations

import json
import os
import datetime as dt
from pathlib import Path
from typing import Any

from common import load_json, save_json
from queue_schedule import parse_time
from session_schedule import marked as session_marked
from session_state import discard_pending, discard_pending_queue_file
from review_retry import verify_review_retry


def _quarantine_invalid(config: dict[str, Any], state: Path, path: Path,
                        reason: str, detail: str = "") -> None:
    """Move an unreadable source intact and persist an actionable diagnostic."""
    destination = state / "failed" / path.name
    if destination.exists():
        suffix = 1
        destination = state / "failed" / (path.stem + f".invalid-{suffix}" + path.suffix)
        while destination.exists():
            suffix += 1
            destination = state / "failed" / (path.stem + f".invalid-{suffix}" + path.suffix)
    moved = False
    try:
        os.replace(path, destination)
        moved = True
    except OSError as error:
        detail = detail or type(error).__name__
    diagnostic = {"status": "quarantined", "type": "QueueSourceInvalid", "reason": reason,
                  "queueFile": path.name}
    if detail:
        diagnostic["detail"] = detail[:240]
    save_json(state / "last-error.json", diagnostic)
    save_json(state / "failed" / (path.name + ".diagnostic.json"), diagnostic)
    if moved:
        discard_pending_queue_file(config, path.name)


def load_jobs(state: Path, config: dict[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    """Return valid queue jobs and quarantine corrupt or unidentified files."""
    records = []
    for path in sorted((state / "queue").glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            _quarantine_invalid(config, state, path, "invalid-json")
            continue
        if not isinstance(value, dict):
            _quarantine_invalid(config, state, path, "invalid-shape")
            continue
        if not value.get("id"):
            _quarantine_invalid(config, state, path, "missing-id")
            continue
        try:
            verify_review_retry(config, value, expected_id=path.stem)
        except (OSError, ValueError, KeyError, TypeError) as error:
            _quarantine_invalid(config, state, path, "review-input-drift", str(error))
            continue
        records.append((path, value))
    records.sort(key=lambda item: (parse_time(item[1].get("createdAt")) is None,
        parse_time(item[1].get("createdAt")) or dt.datetime.max.replace(tzinfo=dt.timezone.utc), item[0].name))
    return records


def cleanup_terminal_pending(config: dict[str, Any], state: Path) -> None:
    """Drop scheduling markers for sources quarantined during an earlier restart."""
    for path in sorted((state / "failed").glob("*.json")):
        try:
            job = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(job, dict) and session_marked(job, config):
            discard_pending(config, job)
