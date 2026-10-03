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
import json
from pathlib import Path
from typing import Any, Callable

from common import digest, load_json, page_ids, save_json
from exchange import export_result, import_pending, settings, write_receipt
from publication_hold import hold_unpublishable
from queue_replica import due as replica_due, enabled as replica_enabled
from queue_replica import mark_retry as mark_replica_retry, prepare as prepare_replica
from queue_schedule import clock_value as _clock_value, due_at as _due_at, iso as _iso
from queue_schedule import is_exchange as _is_exchange, parse_time as _parse_time, take_budget as _take_budget
from queue_recovery import (fail_batch, move_terminal_source, reconcile_failed_batches,
                            reconcile_receipts, replay_completed, retry_sources,
                            retry_job as _retry_job, terminal_source, existing_audit as _existing_audit)
from queue_wire import EventTooLarge, checked_request, job_bytes
from replica import PublicationContractError
from queue_intake import cleanup_terminal_pending as _cleanup_terminal_pending
from queue_intake import load_jobs as _load_jobs
from review_capacity import should_wait_for_review
from review_retry import finish_review_retry, prepare_review_retry, verify_prepared_retry, verify_review_retry
from session_schedule import marked as session_marked
from session_state import (commit_batch, context_for_job, discard_pending,
                           reconcile_queue_pending)


DEFAULT_DEBOUNCE_SECONDS = 120
DEFAULT_BATCH_JOBS = 5
DEFAULT_BATCH_BYTES = 120_000
MAX_JOB_BYTES = DEFAULT_BATCH_BYTES
DEFAULT_RETRY_BASE_SECONDS = 300
DEFAULT_PROCESS_TIMEOUT_SECONDS = 1_000


def _state_dirs(state: Path) -> None:
    for name in ("queue", "failed", "completed", "audit", "batches", "review", "exchange-errors"):
        (state / name).mkdir(parents=True, exist_ok=True, mode=0o700)


def _batch_key(job: dict[str, Any]) -> tuple[str, str, str] | None:
    if _is_exchange(job) or job.get("reviewRetryOf") or not job.get("projectId") or not job.get("sessionId"):
        return None
    contract = "session-v1" if ("sessionSchedule" in job or "sessionContext" in job) else "legacy-v1"
    contract += ":" + str(job.get("topicScope", "project"))
    return str(job["projectId"]), str(job["sessionId"]), contract


def _reject_oversize(config, path, job, now, reason="job-byte-limit"):
    """Quarantine a job whose serialized intake cannot fit the model boundary."""
    state = Path(config["stateDir"])
    job = dict(job)
    job.update({"status": "oversize", "error": reason, "jobBytes": job_bytes(job),
                "maxJobBytes": int(config.get("maxJobBytes", MAX_JOB_BYTES)),
                "rejectedAt": _iso(now)})
    move_terminal_source(state, path, job)
    if session_marked(job, config):
        discard_pending(config, job)
    save_json(state / "last-error.json", {"at": _iso(now), "jobId": job.get("id"),
                                           "type": "JobTooLarge", "jobBytes": job["jobBytes"],
                                           "maxJobBytes": job["maxJobBytes"]})


def _oversize_batch(config, audit_path, audit, selected, now, reason):
    """Quarantine a batch rejected at intake and make its audit terminal."""
    try:
        for path, job in selected:
            _reject_oversize(config, path, job, now, reason)
    except BaseException as error:
        audit.update(status="failed", error=type(error).__name__, rejectedAt=_iso(now),
                     remainingQueueFiles=[path.name for path, _ in selected if path.exists()])
        save_json(audit_path, audit)
        raise
    audit.update(status="failed", error="JobTooLarge", rejectedAt=_iso(now))
    save_json(audit_path, audit)
    return {"status": "error", "reason": "oversize", "jobs": len(selected), "attempted": False}


def _job_limit(config) -> int:
    """Read the per-source cap used by intake."""
    return max(1000, int(config.get("maxJobBytes", MAX_JOB_BYTES)))


def _select_batch(records, first, now, config, blocked=None):
    first_path, first_job = first
    key = _batch_key(first_job)
    if key is None:
        return [first]
    max_jobs = max(1, int(config.get("maxBatchJobs", DEFAULT_BATCH_JOBS)))
    max_bytes = max(1000, int(config.get("maxBatchBytes", DEFAULT_BATCH_BYTES)))
    first_created = _parse_time(first_job.get("createdAt"))
    session_batch = session_marked(first_job, config)
    event = config.get("eventDriven", {})
    window = max(0, int(event.get("batchWindowSeconds", config.get("batchWindowSeconds", DEFAULT_DEBOUNCE_SECONDS))))
    selected = [first]
    total = job_bytes(first_job)
    for path, job in records:
        if (len(selected) >= max_jobs or path == first_path or path.name in (blocked or set())
                or _batch_key(job) != key):
            continue
        retry_at = _parse_time(job.get("nextAttemptAt"))
        if retry_at and retry_at > now:
            continue
        if not session_batch and _due_at(job, now, config) > now:
            continue
        created = _parse_time(job.get("createdAt"))
        if not session_batch and first_created and created and abs((created - first_created).total_seconds()) > window:
            continue
        size = job_bytes(job)
        if size > _job_limit(config):
            break
        if total + size > max_bytes:
            break
        selected.append((path, job))
        total += size
    return selected


def _merge_batch(selected, config):
    paths, jobs = zip(*selected)
    if len(jobs) == 1 and (_is_exchange(jobs[0]) or jobs[0].get("reviewRetryOf")):
        merged = dict(jobs[0])
        merged["sourceJobIds"] = [str(jobs[0]["id"])]
        merged["sourceQueueFiles"] = [paths[0].name]
        merged["allowedPageIds"] = page_ids(config, merged["projectId"], topic_scope=merged.get("topicScope", "project"))
        return merged
    identifiers = [str(job["id"]) for job in jobs]
    batch_id = "batch-" + digest("|".join(sorted(identifiers)))[:48]
    merged = dict(jobs[0])
    merged["id"] = batch_id
    merged["sourceJobIds"] = identifiers
    merged["sourceQueueFiles"] = [path.name for path in paths]
    merged["prompt"] = "\n\n--- consecutive turn ---\n\n".join(
        str(job.get("prompt", "")) for job in jobs if job.get("prompt"))
    evidence = []
    seen = set()
    for job in jobs:
        for item in job.get("evidence", []):
            identity = (item.get("id"), item.get("sha256"), item.get("kind"))
            if identity in seen:
                continue
            seen.add(identity)
            evidence.append(item)
    merged["evidence"] = evidence
    merged["allowedPageIds"] = page_ids(config, merged["projectId"], topic_scope=merged.get("topicScope", "project"))
    if session_marked(merged, config) and merged.get("sessionId"):
        merged["sessionContext"] = context_for_job(config, merged["projectId"], merged["sessionId"])
    merged["batchCreatedAt"] = merged.get("createdAt")
    return merged


def _register(config: dict[str, Any], job: dict[str, Any], result: dict[str, Any]) -> None:
    state = Path(config["stateDir"])
    registry = load_json(state / "pages.json", {})
    project = job["projectId"]
    pages = registry.get(project, []) + result.get("publishedPageIds", [])
    registry[project] = sorted(set(pages))
    save_json(state / "pages.json", registry)
    for source_id in job.get("sourceJobIds", [job["id"]]):
        save_json(state / "completed" / (str(source_id) + ".json"), result)


def _finalize(config, audit, result=None) -> dict[str, Any]:
    state = Path(config["stateDir"])
    merged = audit["job"]
    final = result if result is not None else audit.get("result", {})
    verify_prepared_retry(config, merged, expected_id=audit["batchId"])
    if not audit.get("exported"):
        try:
            final = export_result(config, merged, final)
        except PublicationContractError as error:
            audit["unpublishedResult"] = final
            final = hold_unpublishable(config, merged, error)
        audit["exported"] = True
        audit["result"] = final
    write_receipt(config, merged, final)
    _register(config, merged, final)
    if merged.get("sessionContext") and session_marked(merged, config):
        commit_batch(config, merged, final)
    finish_review_retry(config, merged, final)
    for name in audit.get("queueFiles", []):
        (state / "queue" / name).unlink(missing_ok=True)
    audit["status"] = "completed"
    save_json(state / "batches" / (audit["batchId"] + ".json"), audit)
    return final


def _mark_finalize_retry(path, audit, now, config, error):
    attempts = int(audit.get("finalizeAttempts", 0)) + 1
    delay = min(int(config.get("retryBaseSeconds", DEFAULT_RETRY_BASE_SECONDS)) * (2 ** (attempts - 1)), 86400)
    audit.update(status="finalize-retry", finalizeAttempts=attempts, nextFinalizeAt=_iso(now + dt.timedelta(seconds=delay)), error=type(error).__name__)
    save_json(path, audit)


def _frozen_selection(state: Path, records, first_path: Path):
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if audit.get("status") not in ("claimed", "retry", "result-ready", "finalize-retry", "sync-retry"):
            continue
        names = set(audit.get("queueFiles", []))
        if first_path.name not in names or not names:
            continue
        available = {item_path.name: (item_path, job) for item_path, job in records}
        selected = [available[name] for name in audit["queueFiles"] if name in available]
        if len(selected) == len(names):
            return selected
    return None


def _frozen_names(state: Path) -> set[str]:
    names = set()
    for path in (state / "batches").glob("*.json"):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if audit.get("status") in ("claimed", "retry", "result-ready", "finalize-retry", "sync-retry"):
            names.update(audit.get("queueFiles", []))
    return names


def _reuse_durable(config, audit, audit_path, selected, now):
    if "result" not in audit or audit.get("status") == "completed":
        return None
    due = _parse_time(audit.get("nextFinalizeAt"))
    if due and due > now:
        return {"status": "deferred", "reason": "finalize-backoff", "jobs": len(selected), "attempted": False}
    try:
        final = _finalize(config, audit)
        return {"status": final.get("status", "completed"), "result": final,
                "jobs": len(selected), "attempted": False}
    except Exception as error:
        _mark_finalize_retry(audit_path, audit, now, config, error)
        return {"status": "deferred", "reason": "finalize-backoff", "error": type(error).__name__,
                "jobs": len(selected), "attempted": False}


def _claim_batch(config, state, selected, now):
    queue_files = [path.name for path, _ in selected]
    prior = _existing_audit(state, queue_files)
    if prior:
        return prior
    merged = _merge_batch(selected, config)
    path = state / "batches" / (merged["id"] + ".json")
    audit = {"version": 1, "batchId": merged["id"], "status": "claimed",
             "queueFiles": queue_files, "job": merged, "claimedAt": _iso(now)}
    save_json(path, audit)
    return path, audit


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


def _run_batch(config, invoke, selected, now):
    """Replay results first; only fresh work reaches preparation and byte policy."""
    state = Path(config["stateDir"])
    audit_path, audit = _claim_batch(config, state, selected, now)
    try:
        verify_review_retry(config, audit["job"], expected_id=audit["batchId"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _reject_review_drift(state, audit_path, audit, error)
    resumed = _reuse_durable(config, audit, audit_path, selected, now)
    if resumed:
        return resumed
    terminal = [(path, job) for path, job in selected if terminal_source(path, state, job)]
    if terminal:
        for _, job in selected:
            if session_marked(job, config):
                discard_pending(config, job)
        fail_batch(state, audit_path, audit)
        return {"status": "error", "reason": "terminal-source", "jobs": len(selected), "attempted": False}
    too_large_jobs = [(path, job) for path, job in selected if job_bytes(job) > _job_limit(config)]
    if too_large_jobs:
        return _oversize_batch(config, audit_path, audit, too_large_jobs, now, "job-byte-limit")
    batch_bytes = sum(job_bytes(job) for _, job in selected)
    if batch_bytes > max(1000, int(config.get("maxBatchBytes", DEFAULT_BATCH_BYTES))):
        return _oversize_batch(config, audit_path, audit, selected, now, "batch-byte-limit")
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
    if not _take_budget(config, now):
        return {"status": "deferred", "reason": "daily-budget", "jobs": len(selected), "attempted": False}
    return _invoke_batch(config, invoke_config, invoke, audit_path, audit, selected, now)


def _invoke_batch(config, invoke_config, invoke, audit_path, audit, selected, now):
    """Persist model output before attempting publication or source retirement."""
    try:
        verify_prepared_retry(config, audit["job"], expected_id=audit["batchId"])
        result = invoke(invoke_config, "process", {"job": audit["job"]}, DEFAULT_PROCESS_TIMEOUT_SECONDS)
        if (not isinstance(result, dict)
                or result.get("status") not in ("published", "submitted", "empty", "needs_review")):
            raise ValueError("worker returned error")
        audit["status"] = "result-ready"
        audit["result"] = result
        save_json(audit_path, audit)
        final = _finalize(config, audit, result)
        return {"status": final.get("status", "completed"), "result": final, "jobs": len(selected), "attempted": True}
    except Exception as error:  # worker diagnostics stay in local audit, not stdout
        if "result" in audit:
            _mark_finalize_retry(audit_path, audit, now, config, error)
            return {"status": "deferred", "reason": "finalize-error", "error": type(error).__name__, "finalizeErrors": 1, "jobs": len(selected), "attempted": True}
        try:
            return retry_sources(config, audit_path, audit, selected, now, error, _retry_job)
        finally:
            for path, job in selected:
                if session_marked(job, config) and terminal_source(path, Path(config["stateDir"]), job):
                    discard_pending(config, job)

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


def _review_wait(config, state, selected):
    """Unclaimed work for a project with a full review queue waits before any claim.

    Waiting before ``_claim_batch`` keeps no frozen selection, replica basis or
    budget for a wait that can last days; an already claimed batch continues so
    its durable result can still finalize.
    """
    if (_existing_audit(state, [path.name for path, _ in selected])
            or not should_wait_for_review(config, selected[0][1])):
        return None
    return {"status": "deferred", "reason": "review-queue-full", "jobs": len(selected), "attempted": False}


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
    while records and work_count < max(0, int(limit)):
        due = [(path, job) for path, job in records
               if path.name not in postponed
               and (_due_at(job, now, config) <= now or terminal_source(path, state, job))]
        if not due:
            deferred = len(records)
            break
        selected = _select_due(state, records, due, now, config, postponed)
        if not selected:
            deferred = len(records)
            break
        outcome = _review_wait(config, state, selected) or _run_batch(config, invoke, selected, now)
        outcomes.append(outcome)
        work_count += int(outcome.get("attempted", False) or outcome.get("status") != "deferred")
        reconcile_failed_batches(state)
        reconcile_queue_pending(config)
        _cleanup_terminal_pending(config, state)
        records = _load_jobs(state, config)
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
        recovered, finalize_errors = replay_completed(config, now, _finalize, _mark_finalize_retry)
        receipts = reconcile_receipts(config, state)
        result = _drain_locked(config, invoke, state, now, limit, imported, recovered)
        result["receipts"] = receipts
        result["finalizeErrors"] = finalize_errors + result.get("finalizeErrors", 0)
        if finalize_errors:
            result["reason"] = "finalize-error"
        return result
