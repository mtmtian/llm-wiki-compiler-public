"""Small durable queue recovery helpers kept separate from batch orchestration."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from common import load_json, save_json
from exchange import settings, valid_receipt
from queue_schedule import iso as _iso, parse_time as _parse_time


def reconcile_receipts(config: dict[str, Any], state: Path) -> int:
    """Retire exchange submissions whose publisher receipt is already valid."""
    exchange = settings(config)
    if not exchange or not config.get("publishEnabled"):
        return 0
    retired = 0
    root = Path(exchange["root"])
    for path in sorted((state / "queue").glob("*.json")):
        try:
            job = load_json(path)
            if not isinstance(job, dict):
                continue
            identifier = str(job.get("id", ""))[len("exchange-"):]
            if not str(job.get("id", "")).startswith("exchange-") or len(identifier) != 64:
                continue
            receipt = root / "receipts" / (identifier + ".json")
            if valid_receipt(receipt, identifier, config):
                save_json(state / "completed" / path.name, load_json(receipt, {}))
                path.unlink(missing_ok=True)
                retired += 1
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return retired


def retry_job(state: Path, path: Path, job: dict[str, Any], now: dt.datetime,
              config: dict[str, Any]) -> None:
    """Move a failed source to backoff or quarantine without losing its payload."""
    attempts = int(job.get("attempts", 0)) + 1
    job["attempts"] = attempts
    delay = int(config.get("retryBaseSeconds", 300)) * (2 ** (attempts - 1))
    job["nextAttemptAt"] = _iso(now + dt.timedelta(seconds=min(delay, 86400)))
    save_json(state / "last-error.json", {"at": _iso(now), "jobId": job.get("id"),
                                           "type": "PipelineError", "attempts": attempts,
                                           "nextAttemptAt": job["nextAttemptAt"]})
    if attempts >= 3:
        move_terminal_source(state, path, job)
        return
    save_json(path, job)


def move_terminal_source(state: Path, path: Path, job: dict[str, Any]) -> None:
    """Commit a terminal source by rewriting its inode, then atomically moving it."""
    destination = state / "failed" / path.name
    existing = load_json(destination, None) if destination.exists() else None
    if isinstance(existing, dict) and int(existing.get("attempts", 0)) >= int(job.get("attempts", 0)):
        path.unlink(missing_ok=True)
        return
    save_json(path, job)
    os.replace(path, destination)


def terminal_source(path: Path, state: Path, job: dict[str, Any]) -> bool:
    """Return whether a queued source already has terminal failure state."""
    destination = state / "failed" / path.name
    return (int(job.get("attempts", 0)) >= 3 or job.get("status") in (
            "oversize", "batch-failed", "capacity-admission-failed", "pipeline-error")
            or destination.is_file())


def fail_batch(state: Path, audit_path: Path, audit: dict[str, Any]) -> None:
    """Retire the whole frozen batch without changing its evidence or replica basis."""
    audit.update(status="failed", error=audit.get("error", "terminal-source"))
    save_json(audit_path, audit)
    for name in audit.get("queueFiles", []):
        path = state / "queue" / name
        if not path.exists():
            continue
        try:
            job = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            job = {"id": path.stem, "status": "batch-failed", "error": "invalid-source"}
        if not terminal_source(path, state, job):
            job.update(status="batch-failed", error="frozen-batch-source-failed")
        move_terminal_source(state, path, job)


def reconcile_failed_batches(state: Path) -> None:
    """Finish terminal moves after a hard stop, keeping durable results replayable."""
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(audit, dict) or "result" in audit:
            continue
        if audit.get("status") not in ("claimed", "retry", "sync-retry", "failed"):
            continue
        sources = [state / "queue" / name for name in audit.get("queueFiles", [])]
        terminal = False
        for source in sources:
            if not source.exists():
                terminal = True
                break
            try:
                source_job = load_json(source)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                terminal = True
                break
            if terminal_source(source, state, source_job):
                terminal = True
                break
        if audit["status"] == "failed" or terminal:
            fail_batch(state, path, audit)


def retry_sources(config, audit_path, audit, selected, now, error, retry_source,
                  terminal_result=None):
    """Persist retry intent before source updates, and reconcile interrupted moves."""
    state = Path(config["stateDir"])
    terminal_failure = isinstance(terminal_result, dict)
    audit.update(status="failure-finalize" if terminal_failure else "retry",
                 error=type(error).__name__)
    if terminal_failure:
        audit.update(result=terminal_result, terminalFailure=True)
    save_json(audit_path, audit)
    try:
        for path, job in selected:
            retry_source(state, path, job, now, config)
    except BaseException as interrupted:
        present = {path.name for path, _ in selected if path.exists()}
        complete = present == set(audit.get("queueFiles", []))
        terminal = any(terminal_source(path, state, job) for path, job in selected)
        if terminal_failure:
            audit.update(status="failure-finalize", sourceFinalizeError=(
                type(interrupted).__name__ if complete else "interrupted-partial"),
                remainingQueueFiles=sorted(present))
        else:
            audit.update(status="failed" if terminal or not complete else "retry",
                         error=type(interrupted).__name__ if complete else "interrupted-partial",
                         remainingQueueFiles=sorted(present))
        save_json(audit_path, audit)
        raise
    if terminal_failure:
        audit["status"] = "failure-finalize"
        audit.pop("remainingQueueFiles", None)
        save_json(audit_path, audit)
        return {"status": "error", "error": type(error).__name__,
                "jobs": len(selected), "attempted": True}
    audit["status"] = "failed" if any(not path.exists() for path, _ in selected) else "retry"
    save_json(audit_path, audit)
    if audit["status"] == "failed":
        fail_batch(state, audit_path, audit)
    return {"status": "error", "error": type(error).__name__, "jobs": len(selected), "attempted": True}


def existing_audit(state: Path, queue_files: list[str]):
    expected = set(queue_files)
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if (audit.get("status") in ("claimed", "retry", "result-ready", "finalize-retry",
                                    "sync-retry", "capacity-deferred", "failure-finalize")
                and set(audit.get("queueFiles", [])) == expected):
            return path, audit
    return None
