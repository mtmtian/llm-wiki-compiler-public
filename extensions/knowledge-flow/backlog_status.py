"""Summarize review and capacity-wait backlog per project for read-only health reports.

Pending review capacity may be set high enough that intake is never blocked, so
the backlog has to be visible elsewhere.  This module only reads local state: it
counts current review lineage heads, groups the already-computed unresolved
queue-full audit holds, and reports capacity waits kept in ``capture-pending``
together with the age of the oldest item.  Corrupt or unreadable files are
skipped so a health check never fails because of one bad record.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from common import load_json
from current_reviews import current_reviews

REVIEW_BACKLOG_THRESHOLD = 10
SECONDS_PER_HOUR = 3600
AGE_PRECISION_DIGITS = 1
UTC = dt.timezone.utc


def _parse_time(value: Any) -> dt.datetime | None:
    """Parse an ISO timestamp, treating naive values as UTC and bad values as unknown."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _oldest_age_hours(created: list[dt.datetime], at: dt.datetime) -> float | None:
    """Age of the earliest timestamp in hours, or None when no timestamp is usable."""
    if not created:
        return None
    seconds = max(0.0, (at - min(created)).total_seconds())
    return round(seconds / SECONDS_PER_HOUR, AGE_PRECISION_DIGITS)


def reviews_by_project(state: Path, at: dt.datetime) -> dict[str, dict[str, Any]]:
    """Count current review heads per project with the age of the oldest by createdAt."""
    grouped: dict[str, list[dict]] = {}
    for _, review in current_reviews(state):
        project = review.get("projectId")
        if isinstance(project, str) and project:
            grouped.setdefault(project, []).append(review)
    return {project: {"current": len(items),
                      "oldestAgeHours": _oldest_age_hours(
                          [t for t in (_parse_time(item.get("createdAt")) for item in items) if t], at)}
            for project, items in sorted(grouped.items())}


def queue_full_by_project(unresolved: list[dict]) -> dict[str, int]:
    """Group unresolved queue-full audit holds, as listed by audit status, by project."""
    counts: dict[str, int] = {}
    for item in unresolved:
        project = item.get("projectId")
        if isinstance(project, str) and project:
            counts[project] = counts.get(project, 0) + 1
    return dict(sorted(counts.items()))


# Reasons that mean a complete source is waiting for capacity rather than for its own transcript.
CAPACITY_REASONS = frozenset({"review-queue-full", "queue-full"})


def _capacity_wait(path: Path) -> tuple[str, str, dt.datetime | None] | None:
    """Read one pending record that waits for capacity as (projectId, reason, createdAt), else None."""
    try:
        value = load_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    reason = str(value.get("reason") or "")
    # A capacity record always waits for capacity; an ordinary retry only once its reason says the queue is full.
    if value.get("kind") != "capacity" and reason not in CAPACITY_REASONS:
        return None
    owner = value.get("job") if value.get("kind") == "capacity" else value.get("record")
    project = owner.get("projectId") if isinstance(owner, dict) else None
    if not isinstance(project, str) or not project:
        return None
    return project, reason or "unknown", _parse_time(value.get("createdAt"))


def capture_waits_by_project(state: Path, at: dt.datetime) -> dict[str, dict[str, Any]]:
    """Count capacity waits per project; reason and age come from the oldest dated wait."""
    grouped: dict[str, list[tuple[str, dt.datetime | None]]] = {}
    for path in sorted((state / "capture-pending").glob("*.json")):
        wait = _capacity_wait(path)
        if wait:
            grouped.setdefault(wait[0], []).append((wait[1], wait[2]))
    result = {}
    for project, items in sorted(grouped.items()):
        dated = sorted((created, reason) for reason, created in items if created)
        result[project] = {"count": len(items), "reason": dated[0][1] if dated else items[0][0],
                           "oldestAgeHours": _oldest_age_hours([created for created, _ in dated], at)}
    return result


def backlog_summary(state: Path, unresolved_queue_full: list[dict] | None = None,
                    at: dt.datetime | None = None) -> dict[str, Any]:
    """Return the per-project backlog block shared by --check and --status."""
    state, at = Path(state), at or dt.datetime.now(UTC)
    return {"reviewsByProject": reviews_by_project(state, at),
            "unresolvedQueueFullByProject": queue_full_by_project(unresolved_queue_full or []),
            "captureWaitsByProject": capture_waits_by_project(state, at)}


def has_review_backlog(backlog: dict[str, Any]) -> bool:
    """True when any project holds many current reviews or any capacity wait exists."""
    reviews = backlog.get("reviewsByProject", {}).values()
    return (any(item["current"] >= REVIEW_BACKLOG_THRESHOLD for item in reviews)
            or bool(backlog.get("captureWaitsByProject")))
