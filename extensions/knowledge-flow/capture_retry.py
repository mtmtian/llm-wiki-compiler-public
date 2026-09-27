"""Durable retries for native transcript capture races.

The Stop hook writes an immutable event and route snapshot here when the
named rollout is not complete yet.  A later wake rereads only that named
rollout, never current workspace artifacts, and either enqueues the original
turn once or moves a bounded diagnostic to ``capture-errors``.
"""

from __future__ import annotations

import datetime as dt
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from capture import capture_evidence_result
from common import load_json, safe_text, save_json

UTC = dt.timezone.utc
DEFAULT_ATTEMPTS = 6
DEFAULT_MAX_AGE = 1800
DEFAULT_BASE = 5
DEFAULT_MAX_BACKOFF = 300
EVENT_KEYS = ("session_id", "turn_id", "cwd", "hook_event_name", "prompt",
              "last_assistant_message", "transcript_path", "conversation_evidence",
              "routing_evidence")


def _clock(value: dt.datetime | None = None) -> dt.datetime:
    """Normalize optional test clocks to aware UTC."""
    value = value or dt.datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: dt.datetime) -> str:
    """Serialize a UTC timestamp for local state and wake scheduling."""
    return value.astimezone(UTC).isoformat()


def _parse(value: Any) -> dt.datetime | None:
    """Parse an ISO timestamp without making malformed state actionable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _policy(config: dict[str, Any]) -> dict[str, int]:
    """Read the bounded retry policy from the versioned nested config."""
    value = config.get("captureRetry") if isinstance(config.get("captureRetry"), dict) else {}
    return {"maxAttempts": min(12, max(1, int(value.get("maxAttempts", DEFAULT_ATTEMPTS)))),
            "maxAgeSeconds": min(86400, max(1, int(value.get("maxAgeSeconds", DEFAULT_MAX_AGE)))),
            "baseSeconds": min(300, max(1, int(value.get("baseSeconds", DEFAULT_BASE)))),
            "maxBackoffSeconds": min(3600, max(1, int(value.get("maxBackoffSeconds", DEFAULT_MAX_BACKOFF))))}


def _event_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded hook fields needed for a later exact retry."""
    result = {key: event[key] for key in EVENT_KEYS if key in event and key != "conversation_evidence"}
    if isinstance(result.get("prompt"), str):
        result["prompt"] = safe_text(result["prompt"], 12000)
    if isinstance(result.get("last_assistant_message"), str):
        result["last_assistant_message"] = safe_text(result["last_assistant_message"], 16000)
    return result


def pending_path(config: dict[str, Any], identifier: str) -> Path:
    """Return one private pending capture record path."""
    return Path(config["stateDir"]) / "capture-pending" / (identifier + ".json")


@contextmanager
def _worker_lock(config: dict[str, Any]):
    """Reuse the queue worker ownership lock for capture-only intake."""
    path = Path(config["stateDir"]) / "worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_capture_pending(config: dict[str, Any], identifier: str, event: dict[str, Any],
                           record: dict[str, Any], job: dict[str, Any], reason: str,
                           now: dt.datetime | None = None) -> dict[str, Any]:
    """Persist one immutable source snapshot and schedule its first retry."""
    current = _clock(now)
    path = pending_path(config, identifier)
    existing = load_json(path, {}) if path.exists() else {}
    if isinstance(existing, dict) and existing:
        return existing
    policy = _policy(config)
    snapshot = _event_snapshot(event)
    if "transcript_path" not in snapshot and isinstance(record.get("transcriptPath"), str):
        snapshot["transcript_path"] = record["transcriptPath"]
    value = {"version": 1, "id": identifier, "status": "pending", "attempts": 0,
             "createdAt": _iso(current), "updatedAt": _iso(current),
             "nextAttemptAt": _iso(current + dt.timedelta(seconds=policy["baseSeconds"])),
             "deadlineAt": _iso(current + dt.timedelta(seconds=policy["maxAgeSeconds"])),
             "event": snapshot,
             "record": {key: record.get(key) for key in ("projectId", "reason", "prompt", "repoIdentity", "createdAt", "transcriptPath")},
             "artifactEvidence": list(job.get("evidence", [])), "reason": reason}
    save_json(path, value)
    return value


def _terminal(config: dict[str, Any], value: dict[str, Any], reason: str, when: dt.datetime) -> None:
    """Move an expired retry into the actionable terminal error directory."""
    state = Path(config["stateDir"])
    snapshot_dir = state / "capture-error-snapshots"
    snapshot_path = snapshot_dir / (value["id"] + ".json")
    save_json(snapshot_path, {"event": value.get("event"), "record": value.get("record"),
                              "artifactEvidence": value.get("artifactEvidence", [])})
    diagnostic = {"at": _iso(when), "type": "EvidenceCaptureUnavailable", "status": "error",
                  "jobId": value["id"], "reason": reason, "attempts": value.get("attempts", 0),
                  "createdAt": value.get("createdAt"), "deadlineAt": value.get("deadlineAt"),
                  "snapshotPath": str(snapshot_path)}
    save_json(state / "capture-errors" / (value["id"] + ".json"), diagnostic)
    save_json(state / "last-error.json", diagnostic)
    pending_path(config, value["id"]).unlink(missing_ok=True)


def _reschedule(config: dict[str, Any], value: dict[str, Any], reason: str, when: dt.datetime) -> str:
    """Increase the attempt counter and retain the source until deadline."""
    policy = _policy(config)
    attempts = int(value.get("attempts", 0)) + 1
    deadline = _parse(value.get("deadlineAt")) or when
    if attempts >= policy["maxAttempts"] or when >= deadline:
        value["attempts"] = attempts
        _terminal(config, value, reason, when)
        return "expired"
    delay = min(policy["maxBackoffSeconds"], policy["baseSeconds"] * (2 ** (attempts - 1)))
    value.update({"attempts": attempts, "reason": reason, "updatedAt": _iso(when),
                  "nextAttemptAt": _iso(min(when + dt.timedelta(seconds=delay), deadline))})
    save_json(pending_path(config, value["id"]), value)
    return "pending"


def _pending_records(config: dict[str, Any]):
    """Load only well-shaped pending records; corrupt state remains untouched."""
    directory = Path(config["stateDir"]) / "capture-pending"
    for path in sorted(directory.glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get("id") == path.stem and value.get("event") and value.get("record"):
            yield path, value


def _queue_full(config: dict[str, Any]) -> bool:
    """Respect the normal intake queue cap without creating a false error."""
    state = Path(config["stateDir"])
    return len(list((state / "queue").glob("*.json"))) >= int(config.get("maxQueuedJobs", 30))


def _retry_one(config: dict[str, Any], value: dict[str, Any], when: dt.datetime) -> str:
    """Attempt one pending source and return its bounded outcome."""
    state = Path(config["stateDir"])
    identifier = value["id"]
    if any((state / folder / (identifier + ".json")).exists() for folder in ("queue", "completed", "failed")):
        pending_path(config, identifier).unlink(missing_ok=True)
        return "terminal"
    event, record = dict(value["event"]), dict(value["record"])
    event.setdefault("hook_event_name", "Stop")
    if _queue_full(config):
        return _reschedule(config, value, "queue-full", when)
    captured = capture_evidence_result(event, record, config)
    if captured.get("status") != "ok":
        return _reschedule(config, value, str(captured.get("reason") or "capture-unavailable"), when)
    from hooks import enqueue_job, event_path, prepare_job
    job = prepare_job(event, record, config, captured, artifact_snapshot=value.get("artifactEvidence", []))
    queued = Path(config["stateDir"]) / "queue" / (event_path(config, event).stem + ".json")
    enqueue_job(job, queued, config)
    completed = Path(config["stateDir"]) / "completed" / queued.name
    failed = Path(config["stateDir"]) / "failed" / queued.name
    if queued.exists() or completed.exists() or failed.exists():
        pending_path(config, value["id"]).unlink(missing_ok=True)
        return "recovered" if queued.exists() else "terminal"
    return _reschedule(config, value, "enqueue-not-persisted", when)


def process_capture_retries(config: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    """Retry due pending captures without consuming model budget."""
    when = _clock(now)
    report = {"attempted": 0, "recovered": 0, "pending": 0, "expired": 0, "skipped": 0}
    if config.get("enabled") is False or config.get("intakeEnabled") is False:
        report["reason"] = "intake-disabled"
        return report
    with _worker_lock(config) as acquired:
        if not acquired:
            report["reason"] = "lock-busy"
            return report
        for _, value in _pending_records(config):
            due = _parse(value.get("nextAttemptAt"))
            if due and due > when:
                report["skipped"] += 1
                continue
            report["attempted"] += 1
            outcome = _retry_one(config, value, when)
            if outcome == "recovered":
                report["recovered"] += 1
            elif outcome == "expired":
                report["expired"] += 1
            elif outcome == "pending":
                report["pending"] += 1
    return report


def capture_retry_at(config: dict[str, Any]) -> str | None:
    """Return the earliest retry timestamp for wake scheduling."""
    if config.get("enabled") is False or config.get("intakeEnabled") is False:
        return None
    due = [_parse(value.get("nextAttemptAt")) for _, value in _pending_records(config)]
    values = [item for item in due if item is not None]
    return _iso(min(values)) if values else None


__all__ = ["capture_retry_at", "pending_path", "process_capture_retries", "record_capture_pending"]
