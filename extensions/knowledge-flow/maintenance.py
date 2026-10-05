"""Recover bounded intake and report quality signals without changing selection policy.

Scheduling belongs to the host app. This command is also safe to run manually;
it never treats queue age as approval, and never rewrites a rejected decision.
"""

import argparse
import datetime
import fcntl
import importlib
import json
from pathlib import Path

from common import config_from, load_json, save_json
from hooks import invoke, now, process_queue
from routing import resolve
from exchange import announce_machine, exchange_status
from review_capacity import QUEUE_FULL_ERROR
from current_reviews import current_reviews
from backlog_status import backlog_summary

STATE_COUNT_DIRS = ("queue", "review", "failed", "audit", "completed", "capture-errors",
                    "capture-pending", "exchange-errors", "replica-errors")


def checks(config):
    """Evaluate configured real routing cases and retrieval scope without model calls."""
    results = []
    for case in config.get("checks", []):
        project, _ = resolve(case["cwd"], case["prompt"], case.get("binding"), config)
        results.append({"name": case["name"], "passed": project == case.get("projectId")})
    value = invoke(config, "context", {"projectId": "none", "prompt": "project decision",
                   "allowedPageIds": [], "seen": {}}, 8)
    results.append({"name": "empty-scope-no-context", "passed": not value.get("context")})
    return results


def prune_turns(state):
    """Expire raw hook prompts after seven days; review evidence and audit remain available."""
    cutoff = datetime.datetime.now().timestamp() - 7 * 86400
    for folder in ("turns", "sessions", "context-diagnostics"):
        for path in (state / folder).glob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def context_read_summary(state):
    """Aggregate turn and lifecycle observations without exposing source text."""
    summary = {"counts": {}, "prepared": 0, "preparedEvidenceTurns": 0,
               "matchingStopTurns": 0, "finalMessageObservedTurns": 0,
               "explicitReferenceTurns": 0, "referenceUnknownTurns": 0,
               "lifecycleRecords": 0, "legacyRecords": 0, "invalid": 0,
               "latestObservedAt": None, "delivery": "unverified", "adoption": "unverified"}
    folder = state / "context-diagnostics"
    latest_stamp = None
    for path in folder.glob("*.json"):
        try:
            value = load_json(path, None)
            if not isinstance(value, dict):
                raise ValueError("diagnostic must be an object")
            observed = value.get("observedAt") or value.get("at")
            stamp = _observed_timestamp(observed, path)
            if stamp is not None and (latest_stamp is None or stamp > latest_stamp):
                latest_stamp = stamp
                summary["latestObservedAt"] = observed or datetime.datetime.fromtimestamp(
                    stamp, datetime.timezone.utc).isoformat()
            _add_context_observation(summary, value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            summary["invalid"] += 1
    return summary


def _add_context_observation(summary, value):
    """Count only identified turns; lifecycle and legacy records stay outside turn denominators."""
    kind = value.get("kind")
    if kind is None:
        summary["legacyRecords"] += 1
        return
    if kind == "lifecycle":
        summary["lifecycleRecords"] += 1
        return
    if kind != "turn":
        summary["invalid"] += 1
        return
    status = str(value.get("status", "unknown"))
    summary["counts"][status] = summary["counts"].get(status, 0) + 1
    if value.get("preparedEvidence") is not True:
        return
    summary["preparedEvidenceTurns"] += 1
    summary["prepared"] += 1
    if value.get("stopObservedAt"):
        summary["matchingStopTurns"] += 1
    if value.get("stopMessageObserved") is True:
        summary["finalMessageObservedTurns"] += 1
    if value.get("explicitReference") is True:
        summary["explicitReferenceTurns"] += 1
    elif not isinstance(value.get("explicitReference"), bool):
        summary["referenceUnknownTurns"] += 1


def _observed_timestamp(value, path):
    """Parse a bounded observation time, falling back to the private file mtime."""
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _audit_day(value, path):
    """Return the UTC day attached to an audit event, with file mtime as legacy evidence."""
    for key in ("recordedAt", "at", "createdAt", "updatedAt"):
        stamp = value.get(key)
        if isinstance(stamp, str):
            try:
                parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                return parsed.astimezone(datetime.timezone.utc).date().isoformat()
            except ValueError:
                continue
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).date().isoformat()
    except OSError:
        return None


def _retry_continued(state, resolved):
    """Only count a retry as continued when it reached a terminal result or a new review."""
    retry_id = resolved.get("retryJobId")
    result = resolved.get("result", {})
    if resolved.get("action") != "reprocessed" or not isinstance(retry_id, str):
        return False
    if isinstance(result, dict) and result.get("status") in ("empty", "published", "submitted"):
        return True
    if not isinstance(result, dict) or result.get("status") != "needs_review":
        return False
    try:
        review = load_json(state / "review" / (retry_id + ".json"), None)
        if isinstance(review, dict) and review.get("jobId") == retry_id:
            return True
        successor = load_json(state / "resolved" / (retry_id + ".json"), None)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (isinstance(successor, dict) and successor.get("action") in ("dismiss", "dismissed", "reject", "reprocessed")
            and isinstance(successor.get("review"), dict)
            and successor["review"].get("jobId") == retry_id)


def audit_status(state):
    """Summarize persisted audit errors and unresolved audit-only queue-full holds."""
    by_error, by_day, unresolved = {}, {}, []
    for path in sorted((state / "audit").glob("*.json")):
        try:
            value = load_json(path, None)
            if not isinstance(value, dict):
                continue
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        error = value.get("error")
        if isinstance(error, str) and error:
            by_error[error] = by_error.get(error, 0) + 1
            day = _audit_day(value, path)
            if day:
                by_day[day] = by_day.get(day, 0) + 1
        if value.get("status") != "needs_review" or error != QUEUE_FULL_ERROR:
            continue
        job_id = value.get("jobId")
        identifier = job_id if isinstance(job_id, str) and job_id else path.stem
        if (state / "review" / (identifier + ".json")).exists():
            continue
        try:
            resolved = load_json(state / "resolved" / (identifier + ".json"), {})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            resolved = {}
        if isinstance(resolved, dict) and (resolved.get("action") in ("dismiss", "dismissed")
                                           or _retry_continued(state, resolved)):
            continue
        unresolved.append({"jobId": identifier, "projectId": value.get("projectId"),
                           "error": error, "day": _audit_day(value, path)})
    return {"errorsByType": by_error, "errorsByDay": by_day,
            "unresolvedQueueFullCount": len(unresolved), "unresolvedQueueFull": unresolved}


def active_review_summary(state):
    """Report only current review lineage heads while counts retain raw files."""
    return [{"jobId": identifier, "projectId": review.get("projectId")}
            for identifier, review in current_reviews(state)]


def status_snapshot(config):
    """Read persisted health without pruning, synchronizing, consulting Git or writing a snapshot."""
    state = Path(config["stateDir"])
    maintenance = load_json(state / "maintenance.json", None)
    replica = load_json(state / "replica/status.json", None)
    active_reviews = active_review_summary(state)
    audit = audit_status(state)
    return {"maintenance": ({"at": maintenance.get("at"), "counts": maintenance.get("counts", {})}
                            if isinstance(maintenance, dict) else None),
            "counts": {name: len(list((state / name).glob("*.json"))) for name in STATE_COUNT_DIRS},
            "activeReviewCount": len(active_reviews), "activeReviews": active_reviews,
            "contextRead": context_read_summary(state), "audit": audit,
            "backlog": backlog_summary(state, audit["unresolvedQueueFull"]),
            "replica": replica if isinstance(replica, dict) else None,
            "lastSuccessfulSyncAt": replica.get("lastSuccessfulSyncAt")
            if isinstance(replica, dict) else None}


def report(config, evaluate=False):
    """Keep a compact, private report suitable for the host's maintenance digest."""
    state = Path(config["stateDir"])
    prune_turns(state)
    counts = {name: len(list((state / name).glob("*.json"))) for name in STATE_COUNT_DIRS}
    reviews = [{"file": str(path), "projectId": load_json(path, {}).get("projectId")}
               for path in sorted((state / "review").glob("*.json"))]
    active_reviews = active_review_summary(state)
    result = {"at": now(), "counts": counts, "review": reviews,
              "activeReviewCount": len(active_reviews), "activeReviews": active_reviews,
              "contextRead": context_read_summary(state),
              "backlog": backlog_summary(state, audit_status(state)["unresolvedQueueFull"])}
    if evaluate:
        result["checks"] = checks(config)
        shared = load_json(state / "replica/status.json", {}).get("sharedMaterialization", {})
        result["checks"].append({"name": "shared-materialization", "passed": shared.get("status") != "error"})
    result["exchange"] = exchange_status(config)
    save_json(state / "maintenance.json", result)
    return result


def _parser():
    """Declare the explicit maintenance operations and their options."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--status", action="store_true", help="read persisted health without running maintenance")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--announce", action="store_true", help="update this machine's shared readiness record")
    parser.add_argument("--semantic-topics", choices=("status", "enable"), help="inspect or activate shared topic organization")
    parser.add_argument("--knowledge-ledger", choices=("status", "enable"), help="inspect or activate shared ledger records")
    parser.add_argument("--initialize-baseline", action="store_true", help="freeze the original shared Wiki once for v2")
    parser.add_argument("--resolve", metavar="JOB_ID")
    parser.add_argument("--retry-review", metavar="JOB_ID", help="reprocess a held session with its original evidence")
    parser.add_argument("--requeue-failed", metavar="JOB_ID", help="requeue a failed turn as a new job through normal intake")
    parser.add_argument("--dry-run", action="store_true", help="inspect --retry-review or --requeue-failed without enqueueing")
    parser.add_argument("--action", choices=("reject", "dismiss"))
    writer = parser.add_mutually_exclusive_group()
    writer.add_argument('--shared-writer-status', action='store_true')
    writer.add_argument('--request-shared-write', metavar='REQUEST_ID')
    writer.add_argument('--bootstrap-shared-writer', action='store_true')
    parser.add_argument('--apply', action='store_true', help='apply an explicit shared-writer bootstrap, semantic topic or ledger activation')
    return parser


# Operations that a single explicit retry or requeue must not be combined with.
SOLE_OPERATION_FLAGS = ("drain", "resolve", "initialize_baseline", "apply", "announce", "check", "action",
                        "shared_writer_status", "request_shared_write", "bootstrap_shared_writer",
                        "semantic_topics", "knowledge_ledger")


def _run_explicit_job(parser, args, config):
    """Run one explicit held-review retry or failed-turn requeue; each stands alone."""
    flag = "--retry-review" if args.retry_review else "--requeue-failed"
    if (args.retry_review and args.requeue_failed) or any(getattr(args, name) for name in SOLE_OPERATION_FLAGS):
        parser.error(f"{flag} cannot be combined with other operations")
    if args.retry_review:
        from review_retry import retry_review
        result = retry_review(config, args.retry_review, dry_run=args.dry_run)
    else:
        from failed_requeue import requeue_failed
        result = requeue_failed(config, args.requeue_failed, dry_run=args.dry_run)
    print(json.dumps(result))


def main():
    """Only explicit maintenance invocations can drain or evaluate the queue."""
    parser = _parser()
    args = parser.parse_args()
    config = config_from(args.config)
    if args.status:
        _status_command(parser, args, config)
        return
    if args.dry_run and not (args.retry_review or args.requeue_failed):
        parser.error('--dry-run requires --retry-review or --requeue-failed')
    if args.retry_review or args.requeue_failed:
        _run_explicit_job(parser, args, config)
        return
    if _gate_command(parser, args, config) or _writer_command(parser, args, config):
        return
    immutable = config.get("exchange", {}).get("protocolVersion") == 2
    if args.initialize_baseline:
        from replica import initialize_baseline
        initialize_baseline(config)
    if args.resolve:
        if not args.action:
            parser.error("--resolve requires --action")
        print(json.dumps(_resolve_job(config, args.resolve, args.action)))
        return
    if args.drain and config.get("enabled"):
        from capture_retry import process_capture_retries
        process_capture_retries(config)
        process_queue(config)
    if args.announce:
        announce_machine(config, now())
    if immutable and config.get("enabled"):
        from replica import sync_replica
        sync_replica(config)
    result = report(config, args.check)
    print(json.dumps(result, ensure_ascii=False))
    if any(not item["passed"] for item in result.get("checks", [])):
        raise SystemExit(1)


def _status_command(parser, args, config):
    """Keep persisted status independent from every maintenance or mutation operation."""
    if any((args.drain, args.check, args.announce, args.initialize_baseline, args.resolve,
            args.retry_review, args.requeue_failed, args.dry_run, args.action, args.apply,
            args.semantic_topics, args.knowledge_ledger, args.shared_writer_status,
            args.request_shared_write, args.bootstrap_shared_writer)):
        parser.error("--status cannot be combined with other operations")
    print(json.dumps(status_snapshot(config), ensure_ascii=False))


def _resolve_job(config, job_id, action):
    """Serialize explicit dismissals with retry staging under the worker's existing lock."""
    if action != "dismiss":
        return invoke(config, "resolve", {"jobId": job_id, "action": action}, 8)
    state = Path(config["stateDir"])
    if not state.is_dir():
        return invoke(config, "resolve", {"jobId": job_id, "action": action}, 8)
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"resolved": False, "busy": True}
        return invoke(config, "resolve", {"jobId": job_id, "action": action}, 8)


GATE_COMMANDS = {"semantic_topics": ("--semantic-topics", "semantic_scope"),
                 "knowledge_ledger": ("--knowledge-ledger", "ledger_gate")}


def _gate_command(parser, args, config):
    """Expose explicit all-reader activations without coupling them to writer ownership."""
    selected = [key for key in GATE_COMMANDS if getattr(args, key)]
    if not selected:
        return False
    option, module_name = GATE_COMMANDS[selected[0]]
    if (len(selected) > 1 or args.drain or args.resolve or args.initialize_baseline or args.retry_review or args.announce
            or args.check or args.action or args.shared_writer_status or args.request_shared_write or args.bootstrap_shared_writer):
        parser.error(f'{option} cannot be combined with other operations')
    action = getattr(args, selected[0])
    if args.apply and action != "enable":
        parser.error(f'--apply requires {option} enable')
    gate = importlib.import_module(module_name)
    result = gate.activate(config, now(), apply=args.apply) if action == "enable" else gate.status(config)
    print(json.dumps(result, ensure_ascii=False))
    return True


def _writer_command(parser, args, config):
    """Keep authorization and activation separate from normal automatic maintenance."""
    selected = args.shared_writer_status or args.request_shared_write or args.bootstrap_shared_writer
    if args.apply and not args.bootstrap_shared_writer:
        parser.error('--apply requires --bootstrap-shared-writer')
    if not selected:
        return False
    if args.drain or args.resolve or args.initialize_baseline:
        parser.error('shared-writer actions cannot be combined with other write actions')
    from writer_handoff import request_write, status
    from replica import sync_replica
    if args.shared_writer_status:
        result = status(config)
    elif args.request_shared_write:
        result = request_write(config, args.request_shared_write)
        if result['status'] == 'default-writer':
            result = sync_replica(config)['sharedMaterialization']
    else:
        result = sync_replica({**config, '_sharedWriterBootstrap': args.apply})['sharedMaterialization']
    if args.announce:
        announce_machine(config, now())
    print(json.dumps(result, ensure_ascii=False))
    if result.get('status') == 'error':
        raise SystemExit(1)
    return True


if __name__ == "__main__":
    main()
