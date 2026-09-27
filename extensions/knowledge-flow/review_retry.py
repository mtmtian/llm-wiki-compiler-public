"""Explicit, evidence-preserving reprocessing of held session consolidations.

An operator retry is a new attempt, not approval and not a replay of an old
publication. The original review, batch, model cache and session cursor remain
intact. The queue pins today's accepted replica for the new review while using
the exact original evidence; finalization links the old hold to its outcome.
"""

import copy
import fcntl
import json
import re
from pathlib import Path

from common import digest, load_json, page_ids, save_json
from queue_schedule import clock_value, iso
from queue_wire import checked_request, job_bytes


def _identifier(value):
    """Require an exact filename component instead of silently rewriting IDs."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise ValueError("invalid review identifier")
    return value


def _read_source(state, identifier):
    """Only a completed held session with frozen evidence can be reprocessed."""
    review_text = (state / "review" / (identifier + ".json")).read_text()
    batch_text = (state / "batches" / (identifier + ".json")).read_text()
    review, batch = json.loads(review_text), json.loads(batch_text)
    job = batch.get("job", {})
    if (review.get("jobId") != identifier or batch.get("batchId") != identifier
            or job.get("id") != identifier or batch.get("status") != "completed"
            or batch.get("result", {}).get("status") != "needs_review"
            or batch.get("result", {}).get("contribution")
            or review.get("projectId") != job.get("projectId")
            or not isinstance(job.get("sessionContext"), dict)
            or not isinstance(job.get("evidence"), list) or not job["evidence"]):
        raise ValueError("retry requires a completed held session with original evidence")
    return {"review": review, "batch": batch, "reviewHash": digest(review_text),
            "batchHash": digest(batch_text)}


def _retry_job(source, identifier, created):
    """Copy evidence exactly, with new model identity and no stale scheduling or basis."""
    job = copy.deepcopy(source["batch"]["job"])
    for key in ("sourceJobIds", "sourceQueueFiles", "queueFile", "batchCreatedAt", "sessionSchedule",
                "basisRecordIds", "notBefore", "nextAttemptAt", "attempts", "reviewRetryOf", "reviewRetryHash"):
        job.pop(key, None)
    job.update(id="review-" + digest(identifier + source["reviewHash"] + source["batchHash"])[:48],
               reviewRetryOf=identifier, reviewRetryHash=source["reviewHash"],
               createdAt=created, notBefore=created)
    return job


def _stage_retry(config, state, identifier, source, created, dry_run):
    """Freeze a new request before enqueue; a repeated request reuses it verbatim."""
    job = _retry_job(source, identifier, created)
    base_id, attempt = job["id"], 1
    while (state / "failed" / (job["id"] + ".json")).exists():
        attempt += 1
        job["id"] = base_id + "-" + str(attempt)
    retry_id = job["id"]
    manifest_path = state / "review-retries" / (retry_id + ".json")
    prior = load_json(manifest_path, None)
    if prior:
        job = prior["job"]
    if job_bytes(job) > int(config.get("maxJobBytes", 120000)):
        raise ValueError("review retry exceeds job byte limit")
    checked_request(config, "process", {"job": job})
    existing = next((folder for folder in ("queue", "completed", "failed")
                     if (state / folder / (retry_id + ".json")).exists()), None)
    if not existing and len(list((state / "queue").glob("*.json"))) >= int(config.get("maxQueuedJobs", 30)):
        raise ValueError("queue capacity reached")
    if not dry_run and not existing:
        save_json(manifest_path, prior or {"version": 1, "originalJobId": identifier,
                  "createdAt": created, "job": job, **source})
        save_json(state / "queue" / (retry_id + ".json"), job)
    return {"status": existing or ("ready" if dry_run else "queued"), "reviewJobId": identifier,
            "retryJobId": retry_id, "projectId": job["projectId"], "jobBytes": job_bytes(job)}


def retry_review(config, job_id, dry_run=False, clock=None):
    """Prepare one explicit retry under the worker lock; dry-run does not mutate state."""
    identifier = _identifier(job_id)
    if not config.get("enabled") or not config.get("intakeEnabled", True):
        raise ValueError("intake disabled")
    state = Path(config["stateDir"])
    created = iso(clock_value(clock))
    if dry_run:
        return _stage_retry(config, state, identifier, _read_source(state, identifier), created, True)
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "reviewJobId": identifier}
        return _stage_retry(config, state, identifier, _read_source(state, identifier), created, False)


def _frozen_input(job):
    """Compare input independently of worker-owned scheduling and replica fields."""
    mutable = {"notBefore", "nextAttemptAt", "attempts", "allowedPageIds", "basisRecordIds",
               "sourceJobIds", "sourceQueueFiles"}
    return {key: value for key, value in job.items() if key not in mutable}


def _verify_job(frozen, job, retry_id):
    """Bind both queued and durable jobs to the operator's frozen request."""
    if (job.get("id") != retry_id or _frozen_input(job) != _frozen_input(frozen.get("job", {}))
            or job.get("sourceJobIds", [retry_id]) != [retry_id]
            or job.get("sourceQueueFiles", [retry_id + ".json"]) != [retry_id + ".json"]):
        raise ValueError("review retry input changed")


def verify_review_retry(config, job, expected_id=None):
    """Reject source drift before model work and before publication/finalization."""
    state = Path(config["stateDir"])
    retry_id = expected_id or job.get("id", "")
    original = job.get("reviewRetryOf")
    if not original and not str(retry_id).startswith("review-"):
        return None
    retry_id = _identifier(retry_id)
    frozen = load_json(state / "review-retries" / (retry_id + ".json"), {})
    if frozen.get("originalJobId") != original or frozen.get("reviewHash") != job.get("reviewRetryHash"):
        raise ValueError("review retry provenance missing")
    _verify_job(frozen, job, retry_id)
    original = _identifier(original)
    source = state / "review" / (original + ".json")
    if source.exists():
        if digest(source.read_text()) != frozen["reviewHash"]:
            raise ValueError("original review changed during reprocessing")
    elif load_json(state / "resolved" / (original + ".json"), {}).get("retryJobId") != retry_id:
        raise ValueError("original review disappeared")
    if digest((state / "batches" / (original + ".json")).read_text()) != frozen["batchHash"]:
        raise ValueError("original batch changed during reprocessing")
    return frozen


def _scope(job):
    """Keep the worker-owned execution scope separate from queued source input."""
    return {key: job.get(key) for key in ("allowedPageIds", "basisRecordIds")}


def prepare_review_retry(config, audit, pinned_config):
    """Bind the canonical prepared scope before spending budget or invoking a model."""
    job = audit["job"]
    frozen = verify_review_retry(config, job, expected_id=audit["batchId"])
    if not frozen:
        return
    expected = {"allowedPageIds": page_ids(pinned_config, job["projectId"], topic_scope=job.get("topicScope", "project")),
                "basisRecordIds": audit.get("replicaBasis", {}).get("basisRecordIds")}
    scope = _scope(job)
    if scope != expected or ("executionScope" in frozen and frozen["executionScope"] != scope):
        raise ValueError("review retry execution scope changed")
    if "executionScope" not in frozen:
        save_json(Path(config["stateDir"]) / "review-retries" / (job["id"] + ".json"),
                  {**frozen, "executionScope": scope})


def verify_prepared_retry(config, job, expected_id=None):
    """Recheck frozen source and prepared scope at invocation and finalization."""
    frozen = verify_review_retry(config, job, expected_id=expected_id)
    if frozen and frozen.get("executionScope") != _scope(job):
        raise ValueError("review retry execution scope changed or missing")
    return frozen


def finish_review_retry(config, job, result):
    """Retire an unchanged old hold only after its genuine successor is durable."""
    frozen = verify_prepared_retry(config, job)
    if not frozen:
        return
    state = Path(config["stateDir"])
    original, retry_id = job["reviewRetryOf"], job["id"]
    if result.get("status") not in ("empty", "submitted", "published", "needs_review"):
        raise ValueError("review retry is not terminal")
    if result.get("status") == "needs_review" and not (state / "review" / (retry_id + ".json")).is_file():
        raise ValueError("successor review missing")
    source = state / "review" / (original + ".json")
    archive = state / "resolved" / (original + ".json")
    if not source.exists():
        if load_json(archive, {}).get("retryJobId") == retry_id:
            return
        raise ValueError("original review disappeared")
    save_json(archive, {"action": "reprocessed", "resolvedAt": iso(clock_value(None)),
              "retryJobId": retry_id, "review": frozen["review"], "result": result})
    source.unlink()
