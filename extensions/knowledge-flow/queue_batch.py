"""Build, freeze, and size-check compatible durable queue batches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from admission_usage import intake_lock, source_hash
from common import digest, load_json, page_ids, save_json
from queue_recovery import (existing_audit, fail_batch, move_terminal_source)
from queue_schedule import due_at, is_exchange, iso, parse_time
from queue_wire import job_bytes
from capture_retry import persist_capacity_wait
from review_capacity import review_queue_full
from session_schedule import marked as session_marked
from session_state import context_for_job, discard_pending

DEFAULT_DEBOUNCE_SECONDS = 120
DEFAULT_BATCH_JOBS = 5
DEFAULT_BATCH_BYTES = 120_000
MAX_JOB_BYTES = DEFAULT_BATCH_BYTES


def batch_key(job: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return the session key that controls safe adjacent-turn batching."""
    if is_exchange(job) or job.get("reviewRetryOf") or not job.get("projectId") or not job.get("sessionId"):
        return None
    contract = "session-v1" if ("sessionSchedule" in job or "sessionContext" in job) else "legacy-v1"
    contract += ":" + str(job.get("topicScope", "project"))
    return str(job["projectId"]), str(job["sessionId"]), contract


def reject_oversize(config, path, job, now, reason="job-byte-limit"):
    """Quarantine one source that exceeds its per-job request boundary."""
    state = Path(config["stateDir"])
    job = dict(job)
    job.update({"status": "oversize", "error": reason, "jobBytes": job_bytes(job),
                "maxJobBytes": int(config.get("maxJobBytes", MAX_JOB_BYTES)),
                "rejectedAt": iso(now)})
    move_terminal_source(state, path, job)
    if session_marked(job, config):
        discard_pending(config, job)
    save_json(state / "last-error.json", {"at": iso(now), "jobId": job.get("id"),
                                           "type": "JobTooLarge", "jobBytes": job["jobBytes"],
                                           "maxJobBytes": job["maxJobBytes"]})


def oversize_batch(config, audit_path, audit, selected, now, reason):
    """Quarantine a selected batch and make its durable audit terminal."""
    state = Path(config["stateDir"])
    try:
        for path, job in selected:
            reject_oversize(config, path, job, now, reason)
    except BaseException as error:
        audit.update(status="failed", error=type(error).__name__, rejectedAt=iso(now),
                     remainingQueueFiles=[path.name for path, _ in selected if path.exists()])
        save_json(audit_path, audit)
        raise
    audit.update(status="failed", error="JobTooLarge", rejectedAt=iso(now))
    save_json(audit_path, audit)
    return {"status": "error", "reason": "oversize", "jobs": len(selected), "attempted": False}


def job_limit(config) -> int:
    """Read the per-source cap used by intake and batching."""
    return max(1000, int(config.get("maxJobBytes", MAX_JOB_BYTES)))


def select_batch(records, first, now, config, blocked=None):
    """Select adjacent due jobs sharing one project, session, and topic scope."""
    first_path, first_job = first
    key = batch_key(first_job)
    if key is None:
        return [first]
    max_jobs = max(1, int(config.get("maxBatchJobs", DEFAULT_BATCH_JOBS)))
    max_bytes = max(1000, int(config.get("maxBatchBytes", DEFAULT_BATCH_BYTES)))
    first_created = parse_time(first_job.get("createdAt"))
    session_batch = session_marked(first_job, config)
    event = config.get("eventDriven", {})
    window = max(0, int(event.get("batchWindowSeconds", config.get("batchWindowSeconds", DEFAULT_DEBOUNCE_SECONDS))))
    selected, total = [first], job_bytes(first_job)
    for path, job in records:
        if (len(selected) >= max_jobs or path == first_path or path.name in (blocked or set())
                or batch_key(job) != key):
            continue
        retry_at = parse_time(job.get("nextAttemptAt"))
        if retry_at and retry_at > now:
            continue
        if not session_batch and due_at(job, now, config) > now:
            continue
        created = parse_time(job.get("createdAt"))
        if (not session_batch and first_created and created
                and abs((created - first_created).total_seconds()) > window):
            continue
        size = job_bytes(job)
        if size > job_limit(config) or total + size > max_bytes:
            break
        selected.append((path, job))
        total += size
    return selected


def merge_batch(selected, config):
    """Freeze selected source IDs, prompt, evidence, and current page scope."""
    paths, jobs = zip(*selected)
    if len(jobs) == 1 and (is_exchange(jobs[0]) or jobs[0].get("reviewRetryOf")):
        merged = dict(jobs[0])
        merged["sourceJobIds"] = [str(jobs[0]["id"])]
        merged["sourceQueueFiles"] = [paths[0].name]
        merged["allowedPageIds"] = page_ids(config, merged["projectId"], topic_scope=merged.get("topicScope", "project"))
        return merged
    identifiers = [str(job["id"]) for job in jobs]
    batch_id = "batch-" + digest("|".join(sorted(identifiers)))[:48]
    merged = dict(jobs[0])
    merged.update(id=batch_id, sourceJobIds=identifiers,
                  sourceQueueFiles=[path.name for path in paths])
    merged["prompt"] = "\n\n--- consecutive turn ---\n\n".join(
        str(job.get("prompt", "")) for job in jobs if job.get("prompt"))
    evidence, seen = [], set()
    for job in jobs:
        for item in job.get("evidence", []):
            identity = item.get("id"), item.get("sha256"), item.get("kind")
            if identity not in seen:
                seen.add(identity)
                evidence.append(item)
    merged["evidence"] = evidence
    merged["allowedPageIds"] = page_ids(config, merged["projectId"], topic_scope=merged.get("topicScope", "project"))
    if session_marked(merged, config) and merged.get("sessionId"):
        merged["sessionContext"] = context_for_job(config, merged["projectId"], merged["sessionId"])
    merged["batchCreatedAt"] = merged.get("createdAt")
    return merged


def frozen_selection(state: Path, records, first_path: Path):
    """Reconstruct all surviving sources of an existing durable batch."""
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        active = ("claimed", "retry", "result-ready", "finalize-retry", "sync-retry",
                  "capacity-deferred", "failure-finalize")
        names = set(audit.get("queueFiles", [])) if audit.get("status") in active else set()
        if first_path.name not in names or not names:
            continue
        available = {item_path.name: (item_path, job) for item_path, job in records}
        selected = [available[name] for name in audit["queueFiles"] if name in available]
        if len(selected) == len(names):
            return selected
    return None


def frozen_names(state: Path) -> set[str]:
    """List queue sources whose audits already own them."""
    names = set()
    active = ("claimed", "retry", "result-ready", "finalize-retry", "sync-retry",
              "capacity-deferred", "failure-finalize")
    for path in (state / "batches").glob("*.json"):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if audit.get("status") in active:
            names.update(audit.get("queueFiles", []))
    return names


def capacity_ready_names(state: Path, config) -> set[str]:
    """Wake deferred claimed sources early when their project review slot opens."""
    ready = set()
    for path in (state / "batches").glob("*.json"):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if (isinstance(audit, dict) and audit.get("status") == "capacity-deferred"
                and isinstance(audit.get("job"), dict)
                and not review_queue_full(config, audit["job"])):
            ready.update(audit.get("queueFiles", []))
    return ready


def claim_batch(config, state, selected, now):
    """Persist immutable source hashes before any worker or replica operation."""
    queue_files = [path.name for path, _ in selected]
    prior = existing_audit(state, queue_files)
    if prior:
        return prior
    merged = merge_batch(selected, config)
    path = state / "batches" / (merged["id"] + ".json")
    audit = {"version": 1, "batchId": merged["id"], "status": "claimed",
             "queueFiles": queue_files,
             "queueSourceHashes": {source.name: source_hash(job) for source, job in selected},
             "job": merged, "claimedAt": iso(now)}
    save_json(path, audit)
    return path, audit


def review_wait(config, state, selected, now):
    """Park only unclaimed work whose project review capacity is currently full."""
    if existing_audit(state, [path.name for path, _ in selected]) or not review_queue_full(config, selected[0][1]):
        return None
    with intake_lock(config):
        frozen = frozen_names(state)
        if any(path.name in frozen for path, _ in selected):
            return None
        results = [persist_capacity_wait(config, job, "review-queue-full", now=now, source_path=path)
                   for path, job in selected]
    if any(item.get("status") == "error" for item in results):
        return {"status": "error", "reason": "capacity-admission-failed", "jobs": len(selected),
                "attempted": False}
    return {"status": "deferred", "reason": "review-queue-full", "jobs": len(selected),
            "attempted": False}
