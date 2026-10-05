"""Finalize worker results and replay the same durable batch boundary after interruption."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from common import load_json, save_json
from exchange import export_result, write_receipt
from queue_recovery import retry_job as _retry_job, retry_sources
from queue_schedule import clock_value as _clock_value, iso as _iso, parse_time as _parse_time
from queue_wire import EventTooLarge
from replica import PublicationContractError
from review_retry import (finish_review_retry, prepare_review_retry,
                          verify_prepared_retry, verify_review_retry)
from session_schedule import marked as session_marked
from session_state import commit_batch, discard_pending

DEFAULT_PROCESS_TIMEOUT_SECONDS = 1_000
DEFAULT_RETRY_BASE_SECONDS = 300


def _register(config: dict[str, Any], job: dict[str, Any], result: dict[str, Any]) -> None:
    """Record published page IDs and per-source completion snapshots."""
    state = Path(config["stateDir"])
    registry = load_json(state / "pages.json", {})
    registry[job["projectId"]] = sorted(set(registry.get(job["projectId"], [])
                                             + result.get("publishedPageIds", [])))
    save_json(state / "pages.json", registry)
    for source_id in job.get("sourceJobIds", [job["id"]]):
        save_json(state / "completed" / (str(source_id) + ".json"), result)


def finalize(config: dict[str, Any], audit: dict[str, Any], result=None) -> dict[str, Any]:
    """Publish and retire one durable batch, or quarantine its permanent failure."""
    state = Path(config["stateDir"])
    merged = audit["job"]
    final = result if result is not None else audit.get("result", {})
    verify_prepared_retry(config, merged, expected_id=audit["batchId"])
    permanent = (audit.get("terminalFailure")
                 or final.get("status") == "error" and final.get("retryable") is False)
    if permanent:
        return _finalize_permanent_failure(config, audit, final)
    if not audit.get("exported"):
        try:
            final = export_result(config, merged, final)
        except PublicationContractError as error:
            audit["unpublishedResult"] = final
            final = {"status": "error", "retryable": False, "publishedPageIds": [],
                     "reviewCount": 0, "error": f"publication contract failure: {error}"}
            audit.update(result=final, terminalFailure=True, status="failure-finalize")
            save_json(state / "batches" / (audit["batchId"] + ".json"), audit)
            return _finalize_permanent_failure(config, audit, final)
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


def _finalize_permanent_failure(config, audit, result):
    """Export accepted ledger claims, then quarantine technical failure without completion."""
    state = Path(config["stateDir"])
    audit.update(status="failure-finalize", result=result, terminalFailure=True)
    save_json(state / "batches" / (audit["batchId"] + ".json"), audit)
    if result.get("status") == "error" and "ledgerContribution" in result and not audit.get("ledgerExported"):
        exported = export_result(config, audit["job"], result)
        audit.update(ledgerExported=True, ledgerExportResult=exported)
        save_json(state / "batches" / (audit["batchId"] + ".json"), audit)
    for name in audit.get("queueFiles", []):
        path = state / "queue" / name
        if not path.exists():
            continue
        try:
            source = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            source = {"id": path.stem, "status": "batch-failed", "error": "invalid-source"}
        if not isinstance(source, dict):
            source = {"id": path.stem}
        source.update(status="pipeline-error", error=result.get("error", "permanent pipeline error"))
        from queue_recovery import move_terminal_source
        move_terminal_source(state, path, source)
    discard_pending(config, audit["job"])
    audit.update(status="failed", failedAt=_iso(_clock_value(None)))
    save_json(state / "batches" / (audit["batchId"] + ".json"), audit)
    return result


def mark_finalize_retry(path, audit, now, config, error):
    """Persist bounded finalization backoff without losing the result snapshot."""
    attempts = int(audit.get("finalizeAttempts", 0)) + 1
    delay = min(int(config.get("retryBaseSeconds", DEFAULT_RETRY_BASE_SECONDS)) * (2 ** (attempts - 1)), 86400)
    audit.update(status="finalize-retry", finalizeAttempts=attempts,
                 nextFinalizeAt=_iso(now + dt.timedelta(seconds=delay)), error=type(error).__name__)
    save_json(path, audit)


def replay_completed(config: dict[str, Any], now: dt.datetime) -> tuple[int, int]:
    """Replay durable finalization intent without invoking the model a second time."""
    recovered, errors = 0, 0
    state = Path(config["stateDir"])
    for path in sorted((state / "batches").glob("*.json")):
        audit = None
        try:
            audit = load_json(path)
            if not isinstance(audit, dict):
                continue
            due = _parse_time(audit.get("nextFinalizeAt"))
            if (audit.get("status") in ("result-ready", "finalize-retry", "failure-finalize")
                    and (not due or due <= now)):
                finalize(config, audit)
                recovered += len(audit.get("queueFiles", []))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            if not isinstance(audit, dict):
                continue
            try:
                mark_finalize_retry(path, audit, now, config, error)
                errors += 1
            except (OSError, ValueError, TypeError):
                continue
    return recovered, errors


def reuse_durable(config, audit, audit_path, selected, now):
    """Resume an already persisted result without making another worker call."""
    if "result" not in audit or audit.get("status") == "completed":
        return None
    due = _parse_time(audit.get("nextFinalizeAt"))
    if due and due > now:
        return {"status": "deferred", "reason": "finalize-backoff", "jobs": len(selected), "attempted": False}
    try:
        final = finalize(config, audit)
        return {"status": final.get("status", "completed"), "result": final,
                "jobs": len(selected), "attempted": False}
    except Exception as error:
        mark_finalize_retry(audit_path, audit, now, config, error)
        return {"status": "deferred", "reason": "finalize-backoff", "error": type(error).__name__,
                "jobs": len(selected), "attempted": False}


def _retry_invocation(config, audit_path, audit, selected, now, error, last_result=None):
    """Persist retryable FlowResult diagnostics before bounded source retries."""
    if isinstance(last_result, dict):
        audit["lastResult"] = last_result
        save_json(audit_path, audit)
    effective_result = last_result if isinstance(last_result, dict) else audit.get("lastResult")
    terminal_result = effective_result if (
        isinstance(effective_result, dict) and "ledgerContribution" in effective_result
        and any(int(job.get("attempts", 0)) + 1 >= 3 for _, job in selected)) else None
    response = retry_sources(config, audit_path, audit, selected, now, error, _retry_job,
                             terminal_result=terminal_result)
    if terminal_result is not None:
        return _finalize_failure_result(config, audit_path, audit, selected, now, terminal_result)
    return response


def _finalize_failure_result(config, audit_path, audit, selected, now, result):
    """Run durable terminal failure finalization and preserve its result on export errors."""
    try:
        final = finalize(config, audit, result)
        return {"status": "error", "reason": "permanent-error", "result": final,
                "jobs": len(selected), "attempted": True}
    except Exception as error:
        mark_finalize_retry(audit_path, audit, now, config, error)
        return {"status": "deferred", "reason": "finalize-error", "error": type(error).__name__,
                "finalizeErrors": 1, "jobs": len(selected), "attempted": True}


def _handle_worker_result(config, audit_path, audit, selected, now, result, defer_capacity):
    """Route a valid worker response to capacity wait, bounded retry, or finalization."""
    if not isinstance(result, dict):
        return _retry_invocation(config, audit_path, audit, selected, now,
                                 ValueError("worker returned invalid result"))
    status = result.get("status")
    if status == "deferred":
        return defer_capacity(config, audit_path, audit, selected, now, result)
    if status == "error":
        if result.get("retryable") is False:
            audit.update(status="result-ready", result=result, terminalFailure=True)
            save_json(audit_path, audit)
            return _finalize_failure_result(config, audit_path, audit, selected, now, result)
        return _retry_invocation(config, audit_path, audit, selected, now,
                                 ValueError(result.get("error") or "worker returned error"), result)
    if status not in ("published", "submitted", "empty", "needs_review"):
        return _retry_invocation(config, audit_path, audit, selected, now,
                                 ValueError("worker returned error"))
    audit.update(status="result-ready", result=result)
    save_json(audit_path, audit)
    try:
        final = finalize(config, audit, result)
        return {"status": final.get("status", "completed"), "result": final,
                "jobs": len(selected), "attempted": True}
    except Exception as error:
        mark_finalize_retry(audit_path, audit, now, config, error)
        return {"status": "deferred", "reason": "finalize-error", "error": type(error).__name__,
                "finalizeErrors": 1, "jobs": len(selected), "attempted": True}


def invoke_batch(config, invoke_config, invoke, audit_path, audit, selected, now, defer_capacity):
    """Invoke once, then persist and route its result through the batch lifecycle."""
    try:
        verify_prepared_retry(config, audit["job"], expected_id=audit["batchId"])
        result = invoke(invoke_config, "process", {"job": audit["job"]},
                        DEFAULT_PROCESS_TIMEOUT_SECONDS)
    except Exception as error:
        return _retry_invocation(config, audit_path, audit, selected, now, error)
    return _handle_worker_result(config, audit_path, audit, selected, now, result, defer_capacity)
