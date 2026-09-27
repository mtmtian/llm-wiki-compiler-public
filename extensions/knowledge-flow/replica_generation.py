"""Private generation building for the v2 iCloud replica.

This module is deliberately separate from record validation and publication so
the sync path stays bounded.  It only materializes verified packets into a
private generation and switches the local ``current`` symlink after success.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from common import digest, load_json, save_json
from replica_records import baseline_path_allowed, canonical, safe_relative
from replica import STATUS_PATH, _error_records, _read_baseline_cached, read_records
from replica_cleanup import cleanup_generations
from replica_integrity import seal_generation
from replica_recovery import clear_recovered_error, verify_generation
from shared_materialize import promote_generation
from topic_routes import load_topic_projection
from publication_resolutions import load_resolutions, classify_conflicts

MATERIALIZE_TIMEOUT = 300
RESPONSE_FILE = Path(".llmwiki") / "replica-response.json"


def _worker_hash(config: dict[str, Any]) -> str:
    """Include a real materializer release in generation identity when available."""
    worker = config.get("worker")
    if not isinstance(worker, str):
        return ""
    try:
        return hashlib.sha256(Path(worker).read_bytes()).hexdigest()
    except (OSError, ValueError):
        return ""


def _routing_hash(config: dict[str, Any], topic_routes: list[dict[str, Any]],
                  topic_migration: dict[str, Any] | None = None) -> str:
    """Baseline page ownership and reviewed routes affect topic routing."""
    projects = {project: sorted(value.get("pages", []))
                for project, value in config.get("projects", {}).items()}
    return digest(canonical({"projects": projects, "topicRoutes": topic_routes,
                             "topicMigration": topic_migration,
                             **({"topicScope": "semantic"} if config.get("topicScope") == "semantic" else {})}))


def _digest_for(baseline_id: str, records: list[dict[str, Any]], worker_hash: str = "",
                routing_hash: str = "") -> str:
    """Compute an order-independent generation identity."""
    return digest(canonical({"baselineId": baseline_id, "recordIds": sorted(item["id"] for item in records),
                             "workerHash": worker_hash, "routingHash": routing_hash}))


def _copy_baseline(stage: Path, manifest: dict[str, Any]) -> None:
    """Materialize baseline text into a private generation staging directory."""
    for item in manifest["files"]:
        relative = safe_relative(item["path"])
        if not baseline_path_allowed(relative):
            raise ValueError("baseline path is not allowed")
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["text"], encoding="utf-8")


def _invoke_materializer(config: dict[str, Any], stage: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Call the installed worker's bounded v2 materialize command."""
    node, worker = config.get("node"), config.get("worker")
    if not isinstance(node, str) or not isinstance(worker, str):
        raise ValueError("default materializer requires node and worker")
    runtime = copy.deepcopy(config)
    runtime["wikiRoot"] = str(stage)
    environment = os.environ.copy()
    environment["PATH"] = str(Path(node).parent) + ":" + environment.get("PATH", "")
    for key in ("LLMWIKI_EMBEDDING_PROVIDER", "LLMWIKI_EMBEDDING_MODEL", "OLLAMA_EMBEDDINGS_HOST", "LLMWIKI_EMBED_STRICT"):
        if key in config.get("environment", {}):
            environment[key] = str(config["environment"][key])
    request = {"config": runtime, "records": records}
    completed = subprocess.run([node, worker, "materialize"], input=json.dumps(request, ensure_ascii=False),
                               text=True, encoding="utf-8", capture_output=True, env=environment,
                               timeout=MATERIALIZE_TIMEOUT, check=True)
    return _validate_materialize_result(json.loads(completed.stdout))


def _validate_materialize_result(value: Any) -> dict[str, Any]:
    """Require the small worker response contract before swapping generations."""
    if not isinstance(value, dict) or not isinstance(value.get("conflicts", []), list):
        raise ValueError("materializer returned an invalid response")
    pages = value.get("pages", 0)
    if not isinstance(pages, (int, list, dict)):
        raise ValueError("materializer pages value is invalid")
    return {"pages": pages, "conflicts": value.get("conflicts", [])}


def _fully_visible(record_ids: list[str], conflicts: list[Any]) -> list[str]:
    """Exclude records whose claims are conservatively held in a conflict group."""
    blocked: set[str] = set()
    for conflict in conflicts:
        if isinstance(conflict, dict) and isinstance(conflict.get("recordIds"), list):
            blocked.update(item for item in conflict["recordIds"] if isinstance(item, str))
    return [identifier for identifier in record_ids if identifier not in blocked]


def _generation_response(generations: Path, generation_id: str) -> dict[str, Any]:
    """Recover a prior response; missing or corrupt metadata fails closed."""
    generation = generations / generation_id
    snapshot = verify_generation(generations.parent.parent, generation)
    try:
        value = json.loads(snapshot.response)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("generation response metadata is missing or invalid") from error
    return _validate_materialize_result(value)


def _current_is_digest(current: Path, generations: Path, digest_value: str) -> bool:
    """Check that current is the expected private generation symlink."""
    if not current.is_symlink():
        return False
    try:
        return current.resolve(strict=True) == (generations / digest_value).resolve()
    except OSError:
        return False


def _atomic_current(replica: Path, generation: Path) -> None:
    """Atomically point current at a completed generation."""
    current = replica / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("replica current must remain a symlink")
    temporary = replica / f".current-{generation.name}.pending"
    temporary.unlink(missing_ok=True)
    os.symlink(generation, temporary)
    os.replace(temporary, current)


def _write_status(config: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """Persist a private status snapshot and return it."""
    value["errors"] = _error_records(config)
    save_json(Path(config["stateDir"]) / STATUS_PATH, value)
    return value


def _failure_status(config: dict[str, Any], state: Path, current: Path, requested: str | None, error: Exception) -> None:
    """Record failure against the active view, never claiming the requested build was switched."""
    try:
        previous = json.loads((state / STATUS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        previous = {}
    generation_root = _current_root(current)
    active = previous if isinstance(previous, dict) and generation_root == previous.get("generationRoot") else {}
    value = {"digest": active.get("digest"), "baselineId": active.get("baselineId"),
             "count": active.get("count", 0), "recordIds": active.get("recordIds", []),
             "fullyVisibleRecordIds": active.get("fullyVisibleRecordIds", []),
             "generationRoot": generation_root, "conflicts": active.get("conflicts", []),
             "rollbackRoot": _previous_rollback(state),
             "lastError": type(error).__name__, "requestedDigest": requested}
    _write_status(config, value)


def _previous_rollback(state: Path) -> str | None:
    try:
        value = load_json(state / STATUS_PATH, {})
    except (OSError, ValueError, TypeError):
        return None
    root = value.get("rollbackRoot") if isinstance(value, dict) else None
    return root if isinstance(root, str) and Path(root).is_dir() and not Path(root).is_symlink() else None


def _current_root(current: Path) -> str | None:
    if not current.is_symlink():
        return None
    try:
        return str(current.resolve(strict=True))
    except OSError:
        return None


def _build_generation(config: dict[str, Any], baseline: dict[str, Any], records: list[dict[str, Any]],
                      generation_id: str, materialize: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None,
                      generations: Path) -> dict[str, Any]:
    """Build one private generation and preserve current when materialization fails."""
    stage = generations / f"{generation_id}.building"
    final = generations / generation_id
    if stage.is_symlink():
        stage.unlink()
    elif stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, mode=0o700)
    try:
        _copy_baseline(stage, baseline)
        value = materialize(str(stage), records) if materialize else _invoke_materializer(config, stage, records)
        response = _validate_materialize_result(value)
        save_json(stage / RESPONSE_FILE, response)
        seal_generation(stage, generation_id)
        os.replace(stage, final)
        return response
    except Exception:
        if stage.is_symlink():
            stage.unlink(missing_ok=True)
        elif stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def sync_replica(config: dict[str, Any], materialize: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None = None,
                 after_sync: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Switch and register consumers while holding the generation cleanup lock."""
    from replica_records import validate_config
    validate_config(config)
    state = Path(config["stateDir"])
    replica = state / "replica"
    generations = replica / "generations"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    replica.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "replica.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = _sync_locked(config, state, replica, generations, materialize)
        if after_sync is not None:
            after_sync(status)
        return status


def _sync_locked(config: dict[str, Any], state: Path, replica: Path, generations: Path,
                 materialize: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None) -> dict[str, Any]:
    """Run one sync while the caller holds the replica lock."""
    current = replica / "current"
    generation_id: str | None = None
    previous_root = _current_root(current)
    rollback_root = _previous_rollback(state)
    try:
        baseline = _read_baseline_cached(config)
        records = read_records(config)
        resolutions = load_resolutions(config, baseline["snapshotId"], records)
        projection = load_topic_projection(config, baseline["snapshotId"], records)
        topic_routes = projection["topicRoutes"]
        topic_migration = projection.get("topicMigration")
        runtime_config = copy.deepcopy(config)
        runtime_config["topicRoutes"] = topic_routes
        if topic_migration is not None:
            runtime_config["topicMigration"] = copy.deepcopy(topic_migration)
        generation_id = _digest_for(baseline["snapshotId"], records, _worker_hash(config),
                                    _routing_hash(config, topic_routes, topic_migration))
        record_ids = sorted(item["id"] for item in records)
        if _current_is_digest(current, generations, generation_id):
            response = _generation_response(generations, generation_id)
        else:
            response = _switch_generation(runtime_config, baseline, records, generation_id, materialize, generations)
            if previous_root is not None:
                rollback_root = previous_root
    except Exception as error:
        _failure_status(config, state, current, generation_id, error)
        raise
    active_root = str(current.resolve())
    clear_recovered_error(state, Path(active_root))
    cleanup_generations(state, active_root, rollback_root)
    runtime_config['_sharedWriterInputs'] = {
        'recordIds': record_ids, 'routesHash': digest(canonical(projection))}
    shared = _sync_shared(runtime_config, active_root, baseline)
    visible = _fully_visible(record_ids, response["conflicts"])
    result = {"digest": generation_id, "baselineId": baseline["snapshotId"],
              "count": len(records), "recordIds": record_ids, "fullyVisibleRecordIds": visible,
              "generationRoot": active_root, "rollbackRoot": rollback_root, "pages": response["pages"],
              "sharedMaterialization": shared, "conflicts": response["conflicts"]}
    return _write_status(config, classify_conflicts(resolutions, result, records))


def _sync_shared(config: dict[str, Any], generation: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """A shared edit conflict must not freeze private retrieval or peer intake."""
    error_path = Path(config["stateDir"]) / "replica-errors/shared-materialization.json"
    try:
        result = promote_generation(config, generation, baseline)
        error_path.unlink(missing_ok=True)
        return result
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"status": "error", "error": str(error), "generationRoot": generation}
        save_json(error_path, result)
        return result


def _switch_generation(config: dict[str, Any], baseline: dict[str, Any], records: list[dict[str, Any]],
                       generation_id: str, materialize: Callable | None, generations: Path) -> dict[str, Any]:
    """Build or validate a complete directory before switching the current pointer."""
    generations.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = generations / generation_id
    response = (_generation_response(generations, generation_id)
                if final.exists() or final.is_symlink()
                else _build_generation(config, baseline, records, generation_id, materialize, generations))
    if not final.exists():
        raise ValueError("generation build did not produce a directory")
    _atomic_current(generations.parent, final)
    return response
