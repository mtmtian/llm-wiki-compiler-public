"""Explicit requeue of a failed turn source as a new job through the ordinary intake path.

A turn lands in ``failed/`` when the worker gives up after its attempts or when intake refuses a job
over the byte limit, and nothing replays it. An operator requeue copies its evidence into a new job
with a fresh identity, so the failed source and any failed batch audit stay as they were, and passes
it through normal intake: session context is attached as of now, the byte limit applies, and the
substantive-evidence and session scheduling rules hold. A source that intake would still refuse stays
in ``failed/`` untouched. Once intake has decided, the failed source and its capture error move into one
``resolved/`` record that names the new job, so the same turn cannot be requeued twice.
"""

import copy
import fcntl
import re
from pathlib import Path

from common import load_json, save_json
from admission_usage import (can_admit_runnable, can_admit_wait, intake_lock,
                             runnable_capacity_reason)
from hooks import enqueue_job, now, prepare_session_job
from queue_wire import job_bytes
from queue_batch import MAX_JOB_BYTES
from review_capacity import review_queue_full

# Worker-owned scheduling, scope and diagnostic fields; intake recomputes what it needs.
STALE_FIELDS = ("at", "type", "status", "error", "jobId", "jobBytes", "maxJobBytes", "attempts",
                "nextAttemptAt", "notBefore", "queueFile", "sessionSchedule", "sessionContext",
                "allowedPageIds", "topicScope", "basisRecordIds", "sourceJobIds", "sourceQueueFiles",
                "batchCreatedAt")


def _identifier(value):
    """Require an exact queue filename stem instead of silently rewriting IDs."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("invalid failed job identifier")
    return value


def _read_failed(state, identifier):
    """Only a failed turn with its own evidence qualifies; review retries keep their own flow."""
    source = load_json(state / "failed" / (identifier + ".json"), None)
    if (not isinstance(source, dict) or source.get("id") != identifier or source.get("reviewRetryOf")
            or not source.get("projectId") or not isinstance(source.get("evidence"), list) or not source["evidence"]):
        raise ValueError("requeue requires a failed turn source with its evidence")
    return source


def _new_job(state, source):
    """Copy the turn without stale fields, under the first identity no queue folder already uses."""
    job = {key: value for key, value in copy.deepcopy(source).items() if key not in STALE_FIELDS}
    base = source["id"] + "-requeue"
    job["id"], attempt = base, 1
    while any((state / folder / (job["id"] + ".json")).exists()
              for folder in ("queue", "failed", "completed", "capture-pending")):
        attempt += 1
        job["id"] = f"{base}-{attempt}"
    job["requeueOf"] = source["id"]
    return job


def _outcome(state, job):
    """Name what intake did with the new job."""
    if (state / "queue" / (job["id"] + ".json")).exists():
        return "queued"
    if (state / "completed" / (job["id"] + ".json")).exists():
        return "empty"
    pending = load_json(state / "capture-pending" / (job["id"] + ".json"), None)
    if (isinstance(pending, dict) and pending.get("kind") == "capacity"
            and isinstance(pending.get("job"), dict) and pending["job"].get("id") == job["id"]):
        return "deferred"
    failed = load_json(state / "failed" / (job["id"] + ".json"), None)
    if isinstance(failed, dict) and failed.get("status") == "capacity-admission-failed":
        return "capacity-full"
    raise ValueError("intake neither queued nor completed the requeued job")


def _archive(state, identifier, source, job, outcome):
    """Keep the failed source and its capture error in one resolved record, then retire both."""
    capture = state / "capture-errors" / (identifier + ".json")
    save_json(state / "resolved" / (identifier + ".json"), {"action": "requeued", "resolvedAt": now(),
              "requeueJobId": job["id"], "outcome": outcome, "source": source, "captureError": load_json(capture, None)})
    (state / "failed" / (identifier + ".json")).unlink()
    capture.unlink(missing_ok=True)


def _stage(config, state, identifier, dry_run):
    """Refuse what intake would refuse before any write; otherwise enqueue and archive."""
    source = _read_failed(state, identifier)
    job = _new_job(state, source)
    queued = state / "queue" / (job["id"] + ".json")
    preview = copy.deepcopy(job)
    prepare_session_job(preview, queued, config)
    size, limit = job_bytes(preview), max(1000, int(config.get("maxJobBytes", MAX_JOB_BYTES)))
    report = {"failedJobId": identifier, "requeueJobId": job["id"], "projectId": job["projectId"],
              "jobBytes": size, "maxJobBytes": limit}
    if size > limit:
        return {**report, "status": "too-large"}
    maximum = max(0, int(config.get("maxQueuedJobs", 30)))
    needs_wait = (review_queue_full(config, preview)
                  or runnable_capacity_reason(state, maximum) is not None)
    if needs_wait and not can_admit_wait(state, maximum):
        return {**report, "status": "capacity-full"}
    if not needs_wait and not can_admit_runnable(state, maximum):
        return {**report, "status": "queue-full"}
    if dry_run:
        return {**report, "status": "ready"}
    with intake_lock(config):
        needs_wait = (review_queue_full(config, preview)
                      or runnable_capacity_reason(state, maximum) is not None)
        if needs_wait and not can_admit_wait(state, maximum):
            return {**report, "status": "capacity-full"}
        if not needs_wait and not can_admit_runnable(state, maximum):
            return {**report, "status": "queue-full"}
        enqueue_job(job, queued, config, intake_locked=True)
        outcome = _outcome(state, job)
    if outcome == "capacity-full":
        return {**report, "status": outcome}
    _archive(state, identifier, source, job, outcome)
    return {**report, "status": outcome}


def requeue_failed(config, job_id, dry_run=False):
    """Requeue one failed turn under the worker lock; dry-run reports without any state write."""
    identifier = _identifier(job_id)
    if not config.get("enabled") or not config.get("intakeEnabled", True):
        raise ValueError("intake disabled")
    state = Path(config["stateDir"])
    if dry_run:
        return _stage(config, state, identifier, True)
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "failedJobId": identifier}
        return _stage(config, state, identifier, False)
