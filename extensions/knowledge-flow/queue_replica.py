"""Small v2 replica adapter used by the durable queue."""

import datetime as dt
import fcntl
from pathlib import Path
from typing import Any

from common import page_ids, save_json
from replica_recovery import verify_generation

UTC = dt.timezone.utc


def enabled(config: dict[str, Any]) -> bool:
    exchange = config.get("exchange")
    return isinstance(exchange, dict) and exchange.get("protocolVersion") == 2


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def due(audit: dict[str, Any], now: dt.datetime) -> bool:
    scheduled = _parse(audit.get("nextReplicaAt"))
    return scheduled is None or scheduled <= now


def _basis(status: dict[str, Any]) -> dict[str, Any]:
    visible = status.get("fullyVisibleRecordIds")
    root = status.get("generationRoot")
    generation = status.get("digest", status.get("generation"))
    if (not isinstance(visible, list) or any(not isinstance(item, str) for item in visible)
            or not isinstance(root, str) or not root or not isinstance(generation, str) or not generation):
        raise ValueError("replica status has no resolved generation")
    return {"basisRecordIds": list(visible), "generation": generation, "wikiRoot": root}


def _pin_basis(config: dict[str, Any], audit: dict[str, Any], path: Path, status: dict[str, Any]) -> None:
    from replica import read_records
    frozen = _basis(status)
    visible = set(frozen["basisRecordIds"])
    project = audit["job"].get("projectId")
    frozen["basisRecordIds"] = [item["id"] for item in read_records(config)
                                 if item.get("id") in visible
                                 and (audit["job"].get("topicScope") == "semantic"
                                      or item.get("payload", {}).get("projectId") == project)]
    if len(frozen["basisRecordIds"]) > 1024:
        raise ValueError("job basisRecordIds exceeds review limit")
    audit["replicaBasis"] = frozen
    audit["basisRecordIds"] = frozen["basisRecordIds"]
    audit["basisGeneration"] = frozen["generation"]
    audit["wikiRoot"] = frozen["wikiRoot"]
    audit["job"]["basisRecordIds"] = list(frozen["basisRecordIds"])
    pinned = dict(config)
    pinned["wikiRoot"] = frozen["wikiRoot"]
    audit["job"]["allowedPageIds"] = page_ids(pinned, project, topic_scope=audit["job"].get("topicScope", "project"))
    save_json(path, audit)


def prepare(config: dict[str, Any], audit: dict[str, Any], path: Path) -> dict[str, Any]:
    from exchange import require_legacy_importer
    from semantic_scope import require_semantic_job
    require_semantic_job(config, audit["job"])
    require_legacy_importer(config, audit["job"])
    frozen = audit.get("replicaBasis")
    if not isinstance(frozen, dict):
        from replica import sync_replica
        sync_replica(config, after_sync=lambda status: _pin_basis(config, audit, path, status))
        frozen = audit.get("replicaBasis")
        if not isinstance(frozen, dict):
            raise ValueError("replica sync did not pin a durable basis")
    elif (not isinstance(frozen.get("basisRecordIds"), list)
          or any(not isinstance(item, str) for item in frozen.get("basisRecordIds", []))
          or len(frozen.get("basisRecordIds", [])) > 1024
          or not isinstance(frozen.get("wikiRoot"), str)
          or not isinstance(frozen.get("generation"), str)):
        raise ValueError("durable replica basis is invalid")
    else:
        if Path(frozen['wikiRoot']).name != frozen['generation']:
            raise ValueError('durable replica basis generation does not match its directory')
        state = Path(config['stateDir'])
        with (state / 'replica.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            verify_generation(state, Path(frozen['wikiRoot']))
    pinned = dict(config)
    pinned["wikiRoot"] = frozen["wikiRoot"]
    return pinned


def mark_retry(path: Path, audit: dict[str, Any], now: dt.datetime,
               base_seconds: int, error: Exception) -> None:
    attempts = int(audit.get("replicaAttempts", 0)) + 1
    delay = min(max(1, base_seconds) * (2 ** (attempts - 1)), 86400)
    audit.update(status="sync-retry", replicaAttempts=attempts,
                 nextReplicaAt=(now + dt.timedelta(seconds=delay)).astimezone(UTC).isoformat(),
                 error=type(error).__name__)
    save_json(path, audit)
