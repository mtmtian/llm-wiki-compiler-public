"""Durable, event-driven intake for the private knowledge-flow adapter.

The hook owns only immutable intake records.  This module is the single local
drainer: it takes the worker lock, imports complete exchange packets, waits
for debounce/retry times, and invokes the model only for a durable batch.  A
batch audit is written before invocation and completed before source queue
files are removed, so a process interruption can replay the finalisation
without losing a concurrently appended turn.
"""

from __future__ import annotations

import datetime as dt
import fcntl
from pathlib import Path
from typing import Any, Callable

from common import save_json
from admission_usage import can_activate_claimed, intake_lock
from exchange import import_pending, settings
from queue_batch import (DEFAULT_BATCH_BYTES, batch_key as _batch_key,
                         capacity_ready_names as _capacity_ready_names, claim_batch as _claim_batch,
                         frozen_names as _frozen_names, frozen_selection as _frozen_selection,
                         job_limit as _job_limit, oversize_batch as _oversize_batch,
                         review_wait as _review_wait, select_batch as _select_batch)
from queue_finalization import (DEFAULT_RETRY_BASE_SECONDS, invoke_batch as _invoke_batch,
                               replay_completed, reuse_durable as _reuse_durable)
from queue_replica import due as replica_due, enabled as replica_enabled
from queue_replica import mark_retry as mark_replica_retry, prepare as prepare_replica
from queue_schedule import clock_value as _clock_value, due_at as _due_at, iso as _iso
from queue_schedule import parse_time as _parse_time
from queue_schedule import take_budget as _take_budget
from queue_recovery import fail_batch, reconcile_failed_batches, reconcile_receipts, terminal_source
from queue_wire import EventTooLarge, checked_request, job_bytes
from queue_intake import cleanup_terminal_pending as _cleanup_terminal_pending
from queue_intake import load_jobs as _load_jobs
from capture_retry import capacity_backoff_seconds
from review_capacity import review_queue_full
from review_retry import prepare_review_retry, verify_review_retry
from session_schedule import marked as session_marked
from session_state import discard_pending, reconcile_queue_pending


def _state_dirs(state: Path) -> None:
    for name in ("queue", "failed", "completed", "audit", "batches", "review", "exchange-errors"):
        (state / name).mkdir(parents=True, exist_ok=True, mode=0o700)


def _save_late_capacity(config, audit_path, audit, selected, now, result=None):
    """Keep a claimed batch intact and back it off after a late capacity guard."""
    retry_at = _iso(now + dt.timedelta(seconds=capacity_backoff_seconds(config)))
    with intake_lock(config):
        audit.update(status="capacity-deferred", reason="review-queue-full", nextAttemptAt=retry_at)
        if result is not None:
            audit["deferredResult"] = result
        save_json(audit_path, audit)
        for path, job in selected:
            if path.exists():
                job["nextAttemptAt"] = retry_at
                save_json(path, job)
    return {"status": "deferred", "reason": "review-queue-full", "jobs": len(selected),
            "attempted": result is not None, **({"result": result} if result is not None else {})}


def _resume_late_capacity(config, audit_path, audit, selected, now):
    """Wait for a claimed batch's review slot, then resume as soon as it is available."""
    if audit.get("status") != "capacity-deferred":
        return None
    if review_queue_full(config, audit["job"]):
        due = _parse_time(audit.get("nextAttemptAt"))
        if due and due > now:
            return {"status": "deferred", "reason": "review-queue-full", "jobs": len(selected),
                    "attempted": False}
        return _save_late_capacity(config, audit_path, audit, selected, now)
    with intake_lock(config):
        filenames = [path.name for path, _ in selected]
        maximum = max(0, int(config.get("maxQueuedJobs", 30)))
        if not can_activate_claimed(Path(config["stateDir"]), maximum, filenames):
            return {"status": "deferred", "reason": "runtime-queue-full", "jobs": len(selected),
                    "attempted": False}
        audit.update(status="claimed")
        audit.pop("nextAttemptAt", None)
        save_json(audit_path, audit)
    return None


def _prepare_batch(config, audit, audit_path, selected, now):
    if not replica_enabled(config):
        return config, None
    if not replica_due(audit, now):
        return config, {"status": "deferred", "reason": "replica-backoff", "jobs": len(selected), "attempted": False}
    try:
        return prepare_replica(config, audit, audit_path), None
    except Exception as error:
        mark_replica_retry(audit_path, audit, now,
                           int(config.get("retryBaseSeconds", DEFAULT_RETRY_BASE_SECONDS)), error)
        for path, job in selected:
            job["nextAttemptAt"] = audit["nextReplicaAt"]
            save_json(path, job)
        return config, {"status": "deferred", "reason": "replica-sync", "error": type(error).__name__,
                        "replicaErrors": 1, "jobs": len(selected), "attempted": False}


def _reject_review_drift(state, audit_path, audit, error):
    """Keep changed input terminal and preserve its original review for the operator."""
    audit["error"] = str(error)
    fail_batch(state, audit_path, audit)
    return {"status": "error", "reason": "review-input-drift", "jobs": len(audit["queueFiles"]), "attempted": False}


def _reject_terminal_sources(config, state, audit_path, audit, selected):
    """Retire a frozen batch when any source already moved to terminal failure."""
    if not any(terminal_source(path, state, job) for path, job in selected):
        return None
    for _, job in selected:
        if session_marked(job, config):
            discard_pending(config, job)
    fail_batch(state, audit_path, audit)
    return {"status": "error", "reason": "terminal-source", "jobs": len(selected), "attempted": False}


def _enforce_batch_byte_limits(config, audit_path, audit, selected, now):
    """Quarantine selections exceeding either per-source or combined request size."""
    oversized = [(path, job) for path, job in selected if job_bytes(job) > _job_limit(config)]
    if oversized:
        return _oversize_batch(config, audit_path, audit, oversized, now, "job-byte-limit")
    total = sum(job_bytes(job) for _, job in selected)
    if total > max(1000, int(config.get("maxBatchBytes", DEFAULT_BATCH_BYTES))):
        return _oversize_batch(config, audit_path, audit, selected, now, "batch-byte-limit")
    return None


def _run_batch(config, invoke, selected, now):
    """Replay results first; only fresh work reaches preparation and byte policy."""
    state = Path(config["stateDir"])
    audit_path, audit = _claim_batch(config, state, selected, now)
    try:
        verify_review_retry(config, audit["job"], expected_id=audit["batchId"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _reject_review_drift(state, audit_path, audit, error)
    late_wait = _resume_late_capacity(config, audit_path, audit, selected, now)
    if late_wait:
        return late_wait
    resumed = _reuse_durable(config, audit, audit_path, selected, now)
    if resumed:
        return resumed
    terminal = _reject_terminal_sources(config, state, audit_path, audit, selected)
    if terminal:
        return terminal
    oversized = _enforce_batch_byte_limits(config, audit_path, audit, selected, now)
    if oversized:
        return oversized
    invoke_config, deferred = _prepare_batch(config, audit, audit_path, selected, now)
    if deferred:
        return deferred
    try:
        checked_request(invoke_config, "process", {"job": audit["job"]})
        prepare_review_retry(config, audit, invoke_config)
    except EventTooLarge as error:
        audit["wireBytes"] = error.actual
        audit["maxProcessEventBytes"] = error.maximum
        return _oversize_batch(config, audit_path, audit, selected, now, "process-envelope-byte-limit")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _reject_review_drift(state, audit_path, audit, error)
    if review_queue_full(config, audit["job"]):
        return _save_late_capacity(config, audit_path, audit, selected, now)
    if not _take_budget(config, now):
        return {"status": "deferred", "reason": "daily-budget", "jobs": len(selected), "attempted": False}
    return _invoke_batch(config, invoke_config, invoke, audit_path, audit, selected, now,
                         _save_late_capacity)


def _drain_result(outcomes, records, deferred, imported, recovered):
    result = {"processed": sum(item.get("jobs", 0) for item in outcomes
                                if item.get("status") not in ("error", "deferred")),
              "attempts": sum(1 for item in outcomes if item.get("attempted")),
              "batches": len(outcomes), "imported": imported,
              "recovered": recovered, "deferred": deferred, "results": outcomes}
    if not outcomes and not records:
        result["reason"] = "empty"
    for reason in ("review-queue-full", "daily-budget", "finalize-backoff", "finalize-error", "replica-sync",
                   "replica-backoff", "oversize", "batch-byte-limit"):
        if any(item.get("reason") == reason for item in outcomes):
            result["reason"] = reason
    result["finalizeErrors"] = sum(item.get("finalizeErrors", 0) for item in outcomes)
    result["replicaErrors"] = sum(item.get("replicaErrors", 0) for item in outcomes)
    return result


def _select_due(state, records, due, now, config, postponed):
    """Select frozen work first while keeping deferred sessions isolated this wake."""
    blocked = _frozen_names(state) | postponed
    for candidate in due:
        selected = _frozen_selection(state, records, candidate[0])
        if selected:
            return selected
        if candidate[0].name not in blocked:
            return _select_batch(records, candidate, now, config, blocked)
    return None


def _drain_locked(config, invoke, state, now, limit, imported, recovered):
    reconcile_failed_batches(state)
    reconcile_queue_pending(config)
    _cleanup_terminal_pending(config, state)
    records = _load_jobs(state, config)
    outcomes, deferred, work_count, postponed = [], 0, 0, set()
    capacity_ready = _capacity_ready_names(state, config)
    while records and work_count < max(0, int(limit)):
        due = [(path, job) for path, job in records
               if path.name not in postponed
               and (_due_at(job, now, config) <= now or terminal_source(path, state, job)
                    or path.name in capacity_ready)]
        if not due:
            deferred = len(records)
            break
        selected = _select_due(state, records, due, now, config, postponed)
        if not selected:
            deferred = len(records)
            break
        outcome = _review_wait(config, state, selected, now) or _run_batch(config, invoke, selected, now)
        outcomes.append(outcome)
        work_count += int(outcome.get("attempted", False) or outcome.get("status") != "deferred")
        reconcile_failed_batches(state)
        reconcile_queue_pending(config)
        _cleanup_terminal_pending(config, state)
        records = _load_jobs(state, config)
        capacity_ready = _capacity_ready_names(state, config)
        if outcome.get("reason") == "daily-budget":
            deferred = len(records)
            break
        if outcome.get("status") == "deferred":
            key = _batch_key(selected[0][1])
            postponed.update(path.name for path, job in records
                             if path in {item[0] for item in selected}
                             or key is not None and _batch_key(job) == key)
            deferred = len(postponed)
    return _drain_result(outcomes, records, deferred, imported, recovered)


def process_queue(config: dict[str, Any], invoke: Callable | None = None, limit: int = 3,
                  clock: Callable[[], dt.datetime] | None = None, now_fn=None) -> dict[str, Any]:
    """Drain due local work with locking, debounce, retries, and bounded batches."""
    if not config.get("enabled") or not config.get("intakeEnabled", True):
        return {"processed": 0, "reason": "intake-disabled"}
    if invoke is None:
        from hooks import invoke as default_invoke
        invoke = default_invoke
    if now_fn is not None:
        clock = now_fn
    settings(config)
    state = Path(config["stateDir"])
    _state_dirs(state)
    now = _clock_value(clock)
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"processed": 0, "busy": True, "reason": "lock-busy"}
        imported = import_pending(config)
        recovered, finalize_errors = replay_completed(config, now)
        receipts = reconcile_receipts(config, state)
        result = _drain_locked(config, invoke, state, now, limit, imported, recovered)
        result["receipts"] = receipts
        result["finalizeErrors"] = finalize_errors + result.get("finalizeErrors", 0)
        if finalize_errors:
            result["reason"] = "finalize-error"
        return result
