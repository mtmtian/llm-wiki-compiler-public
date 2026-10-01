"""One-shot event wake for knowledge-flow.

LaunchAgent/WatchPaths can invoke this command when a local intake or
publisher submission changes.  It performs a bounded reconciliation, drains
only due work, and records a compact wake state.  The state file is replaced
only when counters or reasons change, so a quiet day does not create a stream
of self-generated filesystem events.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from common import config_from, load_json, save_json
from exchange import announce_machine, exchange_counts
from queue_worker import process_queue
from queue_schedule import next_wake_at, clock_value
from capture_retry import process_capture_retries


UTC = dt.timezone.utc


def _iso(value: dt.datetime) -> str:
    """Format an aware UTC timestamp for wake-state consumers."""
    return value.astimezone(UTC).isoformat()


def _counts(config: dict[str, Any]) -> dict[str, int]:
    """Read private queue counters without invoking a model or mutating state."""
    state = Path(config["stateDir"])
    names = ("queue", "failed", "review", "completed", "audit", "exchange-errors", "capture-errors", "capture-pending", "replica-errors")
    counts = {name: len(list((state / name).glob("*.json"))) for name in names}
    replica = load_json(state / "replica/status.json", {}) or {}
    return {**counts, "replica-records": int(replica.get("count", 0)),
            "replica-conflicts": len(replica.get("conflicts", []))}


def _submission_counts(config: dict[str, Any]) -> dict[str, int]:
    """Include exchange visibility so a lost watcher event is recoverable."""
    status = exchange_counts(config)
    return {"submitted": int(status.get("submitted", 0)),
            "unreceipted": int(status.get("unreceipted", 0))}


def _event_enabled(config: dict[str, Any]) -> bool:
    """Keep a stale LaunchAgent harmless when event-driven intake is disabled."""
    return bool(config.get("enabled") and config.get("eventDriven", {}).get("enabled", False))


def _reasons(before: dict[str, int], after: dict[str, int], drained: dict[str, Any]) -> list[str]:
    """Turn counters and drain outcome into stable, user-readable wake reasons."""
    reasons = []
    if after.get("queue", 0):
        reasons.append("queue-pending")
    if after.get("failed", 0):
        reasons.append("failed-jobs")
    if after.get("review", 0):
        reasons.append("review-pending")
    if after.get("exchange-errors", 0):
        reasons.append("exchange-errors")
    if after.get("capture-errors", 0):
        reasons.append("capture-errors")
    if after.get("capture-pending", 0):
        reasons.append("capture-pending")
    if after.get("replica-errors", 0):
        reasons.append("replica-errors")
    if after.get("replica-conflicts", 0):
        reasons.append("replica-conflicts")
    if after.get("unreceipted", 0):
        reasons.append("unreceipted-submissions")
    if drained.get("busy"):
        reasons.append("lock-busy")
    if drained.get("deferred"):
        reasons.append("retry-or-debounce")
    if drained.get("reason") in ("daily-budget", "review-queue-full"):
        reasons.append(drained["reason"])
    if drained.get("reason") == "event-disabled":
        reasons.append("event-disabled")
    if drained.get("finalizeErrors", 0) or drained.get("replicaErrors", 0) or any(item.get("status") == "error" for item in drained.get("results", [])):
        reasons.append("processing-error")
    common = {key: after.get(key) for key in before}
    if not reasons and before != common:
        reasons.append("state-updated")
    return reasons or ["quiet"]


def _should_write(previous: dict[str, Any], counters: dict[str, int], reasons: list[str], next_wake) -> bool:
    """Avoid rewriting the watched status file for unchanged quiet runs."""
    if not previous:
        return True
    target = _iso(next_wake) if next_wake else None
    return (previous.get("counters") != counters or previous.get("reasons") != reasons
            or previous.get("nextWakeAt") != target)


def _reconcile_replica(config):
    """Refresh a v2 local view even when the only event came from a peer."""
    if config.get("exchange", {}).get("protocolVersion") != 2:
        return
    from replica import sync_replica
    error_path = Path(config["stateDir"]) / "replica-errors/event-sync.json"
    try:
        sync_replica(config)
        error_path.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        save_json(error_path, {"error": type(error).__name__, "operation": "event-sync"})
        raise


def _drain_and_sync(config, enabled, drain, invoke, clock):
    """Hold new model work on replica failure; finish each successful publication locally."""
    if not enabled:
        return {"processed": 0, "attempts": 0, "reason": "event-disabled"}
    drained = {"processed": 0, "attempts": 0, "reason": "reconcile-only"}
    try:
        if drain:
            drained["captureRecovery"] = process_capture_retries(config, now=clock_value(clock))
        _reconcile_replica(config)
        if drain:
            drained.update(process_queue(config, invoke=invoke, clock=clock))
        if drained.get("processed", 0):
            _reconcile_replica(config)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        drained = {**drained, "reason": "processing-error", "finalizeErrors": 1}
    return drained


def run_once(config: dict[str, Any], invoke: Callable | None = None, clock=None,
             drain: bool = True, announce: bool = False) -> dict[str, Any]:
    """Reconcile, drain eligible work once, and persist changed status only."""
    now = clock_value(clock)
    state = Path(config["stateDir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        from maintenance import prune_turns
        prune_turns(state)
    except (ImportError, OSError):
        pass
    before = _counts(config)
    enabled = _event_enabled(config)
    drained = _drain_and_sync(config, enabled, drain, invoke, clock)
    submissions = _submission_counts(config) if enabled else {}
    counters = {**_counts(config), "imported": int(drained.get("imported", 0)),
                "processed": int(drained.get("processed", 0)),
                "attempts": int(drained.get("attempts", 0)),
                "submitted": submissions.get("submitted", 0),
                "unreceipted": submissions.get("unreceipted", 0),
                "receipts": int(drained.get("receipts", 0))}
    reasons = _reasons(before, counters, drained)
    path = state / "wake-state.json"
    previous = load_json(path, {}) or {}
    next_wake = next_wake_at(config, clock=clock) if enabled else None
    changed = _should_write(previous, counters, reasons, next_wake)
    if changed:
        save_json(path, {"at": _iso(now), "nextWakeAt": _iso(next_wake) if next_wake else None,
                         "reasons": reasons, "counters": counters})
    if announce and enabled:
        announce_machine(config, _iso(now))
    result = {"at": _iso(now), "nextWakeAt": _iso(next_wake) if next_wake else None, "reasons": reasons,
              "counters": counters, "changed": changed, "drain": drained}
    return result


def run_with_debounce(config: dict[str, Any], invoke: Callable | None = None, clock=None,
                      sleep_fn: Callable[[float], None] | None = None, announce: bool = False) -> dict[str, Any]:
    """Wait briefly for a same-session burst, then drain all currently due batches."""
    sleep_fn = sleep_fn or time.sleep
    event = config.get("eventDriven", {})
    max_wait = min(120, max(0, int(event.get("debounceSeconds", config.get("debounceSeconds", 120)))))
    started = clock_value(clock)
    waited = 0.0
    result = run_once(config, invoke=invoke, clock=clock, announce=announce)
    while True:
        target = result.get("nextWakeAt")
        if not target:
            break
        due = dt.datetime.fromisoformat(target.replace("Z", "+00:00"))
        current = clock_value(clock)
        remaining = (due - current).total_seconds()
        if remaining <= 0:
            if (result["counters"].get("queue", 0) == 0
                    or any(reason in result["reasons"] for reason in
                    ("daily-budget", "lock-busy", "processing-error", "intake-disabled", "event-disabled"))):
                break
            if result["drain"].get("processed", 0) == 0:
                break
            result = run_once(config, invoke=invoke, clock=clock)
            continue
        elapsed = max(waited, (current - started).total_seconds())
        if elapsed >= max_wait or remaining > max_wait - elapsed:
            break
        pause = min(remaining, max_wait - elapsed, 60)
        sleep_fn(pause)
        waited += pause
        result = run_once(config, invoke=invoke, clock=clock)
    return result


def main(argv=None) -> None:
    """CLI entry point; default invocation always performs one complete wake."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--drain", action="store_true", help="compatibility flag; drain is default")
    parser.add_argument("--announce", action="store_true")
    args = parser.parse_args(argv)
    config = config_from(args.config)
    result = run_with_debounce(config, announce=args.announce)
    try:
        from notify import notify_actionable
        result["notification"] = notify_actionable(config, result)
    except (ImportError, OSError, ValueError):
        result["notification"] = {"sent": False, "reason": "notification-unavailable"}
    print(json.dumps(result, ensure_ascii=False))
    if "processing-error" in result["reasons"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
