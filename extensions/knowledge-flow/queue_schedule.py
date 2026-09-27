"""Clock, debounce, budget, and wake-time policy for the queue worker."""

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from common import load_json, save_json
from session_schedule import due_at as session_due_at, marked as session_marked

UTC = dt.timezone.utc
DEFAULT_DEBOUNCE_SECONDS = 120
DEFAULT_RETRY_BASE_SECONDS = 300


def parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def clock_value(clock: Callable[[], dt.datetime] | None) -> dt.datetime:
    value = clock() if clock else dt.datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_exchange(job: dict[str, Any]) -> bool:
    return str(job.get("id", "")).startswith("exchange-") or job.get("source") == "exchange"


def due_at(job: dict[str, Any], now: dt.datetime, config: dict[str, Any]) -> dt.datetime:
    session_due = session_due_at(job, now, config) if session_marked(job, config) else None
    if session_due is not None:
        retry = parse_time(job.get("nextAttemptAt"))
        return retry if retry else session_due
    for key in ("nextAttemptAt", "notBefore"):
        parsed = parse_time(job.get(key))
        if parsed:
            return parsed
    created = parse_time(job.get("createdAt"))
    if created and not is_exchange(job):
        event = config.get("eventDriven", {})
        delay = int(event.get("debounceSeconds", config.get("debounceSeconds", DEFAULT_DEBOUNCE_SECONDS)))
        return created + dt.timedelta(seconds=max(0, delay))
    return now


def take_budget(config: dict[str, Any], now: dt.datetime) -> bool:
    path = Path(config["stateDir"]) / "daily-budget.json"
    value = load_json(path, {}) or {}
    today = iso(now)[:10]
    used = int(value.get("used", 0)) if value.get("date") == today else 0
    if used >= int(config.get("maxDailyJobs", 12)):
        return False
    save_json(path, {"date": today, "used": used + 1})
    return True


def _queue_jobs(state: Path) -> list[tuple[Path, dict[str, Any]]]:
    result = []
    for path in (state / "queue").glob("*.json"):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("id"):
            result.append((path, value))
    return result


def next_wake_at(config: dict[str, Any], clock=None) -> dt.datetime | None:
    from capture_retry import capture_retry_at
    now = clock_value(clock)
    state = Path(config["stateDir"])
    candidates = []
    capture_due = parse_time(capture_retry_at(config))
    if capture_due:
        candidates.append(max(now, capture_due))
    budget = load_json(state / "daily-budget.json", {}) or {}
    today = iso(now)[:10]
    exhausted = (budget.get("date") == today
                 and int(budget.get("used", 0)) >= int(config.get("maxDailyJobs", 12)))
    records = _queue_jobs(state)
    for _, job in records:
        if exhausted:
            continue
        due = due_at(job, now, config)
        candidates.append(due if due > now else now)
    for path in (state / "batches").glob("*.json"):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(audit, dict):
            continue
        status = audit.get("status")
        if status not in ("result-ready", "finalize-retry", "sync-retry"):
            continue
        key = "nextReplicaAt" if status == "sync-retry" else "nextFinalizeAt"
        due = parse_time(audit.get(key)) or now
        candidates.append(due if due > now else now)
    if exhausted and records:
        tomorrow = (now + dt.timedelta(days=1)).date()
        candidates.append(dt.datetime.combine(tomorrow, dt.time.min, tzinfo=UTC))
    return min(candidates) if candidates else None
