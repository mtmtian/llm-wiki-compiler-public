"""Recover bounded intake and report quality signals without changing selection policy.

Scheduling belongs to the host app. This command is also safe to run manually;
it never treats queue age as approval, and never rewrites a rejected decision.
"""

import argparse
import datetime
import json
from pathlib import Path

from common import config_from, load_json, save_json
from hooks import invoke, now, process_queue
from routing import resolve
from exchange import announce_machine, exchange_status


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


def report(config, evaluate=False):
    """Keep a compact, private report suitable for the host's maintenance digest."""
    state = Path(config["stateDir"])
    prune_turns(state)
    counts = {name: len(list((state / name).glob("*.json")))
              for name in ("queue", "review", "failed", "audit", "completed", "capture-errors", "capture-pending", "exchange-errors", "replica-errors")}
    reviews = [{"file": str(path), "projectId": load_json(path, {}).get("projectId")}
               for path in sorted((state / "review").glob("*.json"))]
    result = {"at": now(), "counts": counts, "review": reviews,
              "contextRead": context_read_summary(state)}
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
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--announce", action="store_true", help="update this machine's shared readiness record")
    parser.add_argument("--semantic-topics", choices=("status", "enable"), help="inspect or activate shared topic organization")
    parser.add_argument("--initialize-baseline", action="store_true", help="freeze the original shared Wiki once for v2")
    parser.add_argument("--resolve", metavar="JOB_ID")
    parser.add_argument("--retry-review", metavar="JOB_ID", help="reprocess a held session with its original evidence")
    parser.add_argument("--dry-run", action="store_true", help="inspect --retry-review without enqueueing")
    parser.add_argument("--action", choices=("reject", "dismiss"))
    writer = parser.add_mutually_exclusive_group()
    writer.add_argument('--shared-writer-status', action='store_true')
    writer.add_argument('--request-shared-write', metavar='REQUEST_ID')
    writer.add_argument('--bootstrap-shared-writer', action='store_true')
    parser.add_argument('--apply', action='store_true', help='apply an explicit shared-writer bootstrap or semantic topic activation')
    return parser


def main():
    """Only explicit maintenance invocations can drain or evaluate the queue."""
    parser = _parser()
    args = parser.parse_args()
    config = config_from(args.config)
    if args.dry_run and not args.retry_review:
        parser.error('--dry-run requires --retry-review')
    if args.retry_review:
        if args.drain or args.resolve or args.initialize_baseline or args.apply or args.announce or args.check or args.action or args.shared_writer_status or args.request_shared_write or args.bootstrap_shared_writer or args.semantic_topics:
            parser.error('--retry-review cannot be combined with other operations')
        from review_retry import retry_review
        print(json.dumps(retry_review(config, args.retry_review, dry_run=args.dry_run)))
        return
    if _topic_command(parser, args, config) or _writer_command(parser, args, config):
        return
    immutable = config.get("exchange", {}).get("protocolVersion") == 2
    if args.initialize_baseline:
        from replica import initialize_baseline
        initialize_baseline(config)
    if args.resolve:
        if not args.action:
            parser.error("--resolve requires --action")
        print(json.dumps(invoke(config, "resolve", {"jobId": args.resolve, "action": args.action}, 8)))
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


def _topic_command(parser, args, config):
    """Expose an explicit all-reader activation without coupling it to writer ownership."""
    if not args.semantic_topics:
        return False
    if (args.drain or args.resolve or args.initialize_baseline or args.retry_review or args.announce or args.check
            or args.action or args.shared_writer_status or args.request_shared_write or args.bootstrap_shared_writer):
        parser.error('--semantic-topics cannot be combined with other operations')
    if args.apply and args.semantic_topics != "enable":
        parser.error('--apply requires --semantic-topics enable')
    from semantic_scope import activate, readiness
    result = (activate(config, now(), apply=args.apply) if args.semantic_topics == "enable"
              else {"topicScope": config.get("topicScope", "project"), **readiness(config)})
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
