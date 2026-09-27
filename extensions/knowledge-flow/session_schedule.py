"""Quiet-period and maximum-wait policy for per-session queue consolidation."""

from __future__ import annotations

import datetime as dt
from typing import Any

from session_state import is_explicit_consolidation, load_session

UTC = dt.timezone.utc
DEFAULT_QUIET_SECONDS = 300
DEFAULT_MAX_WAIT_SECONDS = 1_800


def _seconds(policy: dict[str, Any], key: str, default: int) -> int:
    if not isinstance(policy, dict):
        return default
    try:
        return max(0, int(policy.get(key, default)))
    except (TypeError, ValueError):
        return default


def enabled(config: dict[str, Any], job: dict[str, Any] | None = None) -> bool:
    """Return whether local jobs use session windows; exchange jobs stay isolated."""
    if job and (job.get("reviewRetryOf") or str(job.get("id", "")).startswith("exchange-") or job.get("source") == "exchange"):
        return False
    policy = config.get("sessionConsolidation", {})
    if isinstance(policy, bool):
        return policy
    return bool(policy.get("enabled", True)) if isinstance(policy, dict) else True


def marked(job: dict[str, Any], config: dict[str, Any]) -> bool:
    """Identify jobs created by the new intake path while retaining old queue compatibility."""
    return enabled(config, job) and ("sessionSchedule" in job or "sessionContext" in job)


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _now(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _pending_times(config: dict[str, Any], job: dict[str, Any]) -> tuple[dt.datetime | None, dt.datetime | None, bool]:
    requested = is_explicit_consolidation(job)
    project, session = job.get("projectId"), job.get("sessionId")
    if project and session:
        state = load_session(config, str(project), str(session))
        first = _parse(state.get("firstQueuedAt"))
        latest = _parse(state.get("lastQueuedAt"))
        if first or latest or state.get("explicit"):
            return first, latest, bool(state.get("explicit")) or requested
    schedule = job.get("sessionSchedule", {})
    if isinstance(schedule, dict):
        first = _parse(schedule.get("firstQueuedAt"))
        latest = _parse(schedule.get("lastQueuedAt"))
        explicit = bool(schedule.get("explicit"))
        if first or latest or explicit:
            return first, latest, explicit or requested
    created = _parse(job.get("createdAt"))
    return created, created, requested


def due_at(job: dict[str, Any], now: dt.datetime, config: dict[str, Any]) -> dt.datetime | None:
    """Compute the next eligible time for a session, including explicit flushes."""
    current = _now(now)
    if not marked(job, config):
        return None
    first, latest, explicit = _pending_times(config, job)
    if explicit:
        return current
    if not first and not latest:
        return current
    policy = config.get("sessionConsolidation", {})
    quiet = _seconds(policy, "quietSeconds", DEFAULT_QUIET_SECONDS)
    maximum = _seconds(policy, "maxWaitSeconds", DEFAULT_MAX_WAIT_SECONDS)
    quiet_at = (latest or first) + dt.timedelta(seconds=quiet)
    max_at = (first or latest) + dt.timedelta(seconds=maximum)
    return min(quiet_at, max_at)


def attach(job: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Add a serialisable scheduling snapshot to the immutable source job."""
    job["sessionSchedule"] = {"version": 1, "firstQueuedAt": state.get("firstQueuedAt"),
                              "lastQueuedAt": state.get("lastQueuedAt"), "explicit": bool(state.get("explicit"))}
    return job
