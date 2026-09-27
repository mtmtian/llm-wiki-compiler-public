"""Pure-iCloud, multi-writer knowledge replicas.

Machines publish independently addressed, reviewed records under
``exchange/v2/publications/<machine>/``. Every machine materializes verified
records into a private generation and switches ``stateDir/replica/current``.
An explicitly designated publisher also reconciles the shared Obsidian view;
that optional projection cannot block local retrieval when human edits conflict.
No reader creates a baseline; call :func:`initialize_baseline` explicitly.
"""

from __future__ import annotations

import datetime as _datetime
import json
from pathlib import Path
from typing import Any, Callable

from common import digest, load_json as local_load_json, save_json
from replica_records import (
    MAX_BASELINE_BYTES,
    MAX_PACKET_BYTES,
    baseline_path_allowed,
    canonical,
    load_json as load,
    validate_baseline,
    validate_config,
    validate_no_credentials,
    validate_packet,
    valid_evidence_roles,
)
from shared_files import SharedFiles

STATUS_PATH = Path("replica") / "status.json"
CACHE_BASELINE_PATH = Path("replica-baseline.json")
CACHE_RECORD_DIR = Path("replica-records")
ERROR_DIR = Path("replica-errors")


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _collect_baseline_files(root: Path) -> list[dict[str, str]]:
    """Read only approved text files while leaving the shared tree untouched."""
    candidates: set[Path] = set()
    for folder, suffixes in (("sources", (".md", ".txt")), ("wiki", (".md",))):
        directory = root / folder
        if not directory.is_dir() or directory.is_symlink():
            continue
        for suffix in suffixes:
            candidates.update(path for path in directory.rglob(f"*{suffix}") if path.is_file())
    metadata = root / ".llmwiki"
    for name in ("config.json", "state.json", "schema.json"):
        path = metadata / name
        if path.is_file() and not path.is_symlink():
            candidates.add(path)
    files: list[dict[str, str]] = []
    remaining = MAX_BASELINE_BYTES
    with SharedFiles(root) as shared:
        for path in sorted(candidates, key=lambda value: value.as_posix()):
            if path.is_symlink() or not _inside_real(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if not baseline_path_allowed(relative):
                continue
            if remaining < 0:
                raise ValueError("baseline exceeds maximum bytes")
            content = shared.read(relative, max_bytes=remaining)
            if content is None:
                continue
            remaining -= len(content)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("baseline files must be valid UTF-8") from error
            if relative in {".llmwiki/config.json", ".llmwiki/state.json", ".llmwiki/schema.json"}:
                try:
                    validate_no_credentials(json.loads(text))
                except json.JSONDecodeError as error:
                    raise ValueError("baseline metadata must be valid JSON") from error
            files.append({"path": relative, "text": text, "sha256": digest(text)})
    return files


def _inside_real(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def initialize_baseline(config: dict[str, Any]) -> dict[str, Any]:
    """Create or verify the immutable v2 baseline; this is the only creator."""
    validate_config(config)
    root = Path(config["sharedWikiRoot"])
    if not root.is_dir():
        raise ValueError("sharedWikiRoot must be an existing directory")
    exchange_root = Path(config["exchange"]["root"])
    exchange_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with SharedFiles(exchange_root) as exchange:
        existing = exchange.read("v2/baseline.json", max_bytes=MAX_BASELINE_BYTES)
        if existing is not None:
            return _decode_baseline(existing)
        files = _collect_baseline_files(root)
        manifest = {"version": 2, "snapshotId": digest(canonical(files)), "files": files}
        encoded = (canonical(manifest) + "\n").encode("utf-8")
        if len(encoded) > MAX_BASELINE_BYTES:
            raise ValueError("baseline exceeds 64 MiB; reduce approved source content before initializing")
        exchange.update("v2/baseline.json", encoded, None, digest(encoded.decode("utf-8"))[:32])
        return validate_baseline(manifest)


def read_baseline(config: dict[str, Any]) -> dict[str, Any]:
    """Read and verify an existing baseline without creating or mutating it."""
    validate_config(config)
    exchange_root = Path(config["exchange"]["root"])
    with SharedFiles(exchange_root) as exchange:
        encoded = exchange.read("v2/baseline.json", max_bytes=MAX_BASELINE_BYTES)
    if encoded is None:
        raise FileNotFoundError("v2/baseline.json")
    return _decode_baseline(encoded)


def _decode_baseline(encoded: bytes) -> dict[str, Any]:
    """Parse one securely read baseline manifest and enforce its size bound."""
    if len(encoded) > MAX_BASELINE_BYTES:
        raise ValueError("invalid or oversized replica file")
    try:
        manifest = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid replica JSON") from error
    return validate_baseline(manifest)


def _read_baseline_cached(config: dict[str, Any]) -> dict[str, Any]:
    """Prefer the shared baseline and fall back to a verified private cache."""
    cache = Path(config["stateDir"]) / CACHE_BASELINE_PATH
    cached = None
    if cache.exists() and not cache.is_symlink():
        try:
            cached = validate_baseline(local_load_json(cache))
        except (OSError, ValueError, TypeError, KeyError):
            _error(config, "replica-baseline.json", "InvalidCachedBaseline")
    try:
        manifest = read_baseline(config)
        if cached and cached["snapshotId"] != manifest["snapshotId"]:
            _error(config, "v2/baseline.json", "BaselineSnapshotChanged")
            return cached
        save_json(cache, manifest)
        _clear_error(config, "v2/baseline.json")
        _clear_error(config, "replica-baseline.json")
        return manifest
    except (OSError, ValueError, KeyError, TypeError):
        _error(config, "v2/baseline.json", "BaselineUnavailable")
        if cached is None:
            raise
        return cached


def _error(config: dict[str, Any], location: str, code: str) -> None:
    """Keep a bounded local diagnostic for malformed or transient iCloud files."""
    identifier = digest(location)
    save_json(Path(config["stateDir"]) / ERROR_DIR / f"{identifier}.json",
              {"at": _now(), "location": location, "error": code})


def _clear_error(config: dict[str, Any], location: str) -> None:
    """Forget a transient diagnostic after the complete shared file recovers."""
    try:
        (Path(config["stateDir"]) / ERROR_DIR / f"{digest(location)}.json").unlink()
    except FileNotFoundError:
        pass


def _error_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Read local diagnostics for status output without changing them."""
    records: list[dict[str, Any]] = []
    directory = Path(config["stateDir"]) / ERROR_DIR
    for path in sorted(directory.glob("*.json")):
        try:
            value = local_load_json(path, {})
            if isinstance(value, dict):
                records.append(value)
        except (OSError, ValueError, TypeError):
            continue
    return records[-100:]


def sync_replica(config: dict[str, Any], materialize: Callable[[str, list[dict[str, Any]]], dict[str, Any]] | None = None,
                 after_sync: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Switch a local generation and optionally pin its consumer before unlocking."""
    from replica_generation import sync_replica as run_sync
    from semantic_scope import apply_scope, require_ready
    config = apply_scope(config)
    if config.get("topicScope") == "semantic":
        require_ready(config)
    return run_sync(config, materialize, after_sync)


def _cache_packet(config: dict[str, Any], packet: dict[str, Any]) -> None:
    """Persist one verified packet locally so temporary iCloud loss is survivable."""
    destination = Path(config["stateDir"]) / CACHE_RECORD_DIR / f"{packet['id']}.json"
    save_json(destination, packet)


def _read_cached_packets(config: dict[str, Any], baseline_id: str) -> dict[str, dict[str, Any]]:
    """Load only previously verified packets from the private cache."""
    result: dict[str, dict[str, Any]] = {}
    directory = Path(config["stateDir"]) / CACHE_RECORD_DIR
    participants = set(config["exchange"]["participants"])
    for path in sorted(directory.glob("*.json")):
        try:
            raw = load(path, MAX_PACKET_BYTES)
            payload = raw.get("payload") if isinstance(raw, dict) else None
            machine = payload.get("machineId") if isinstance(payload, dict) else None
            if machine not in participants:
                raise ValueError("cache participant is undeclared")
            packet = validate_packet(raw, machine, baseline_id)
            if path.stem != packet["id"]:
                raise ValueError("cache filename does not match packet id")
            if not _project_is_allowed(packet["payload"], config):
                raise ValueError("cached publication project is not allowed")
            result[packet["id"]] = packet
            _clear_error(config, f"cache/{path.name}")
        except (OSError, ValueError, KeyError, TypeError):
            _error(config, f"cache/{path.name}", "InvalidCachedRecord")
    return result


def _publication_files(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Enumerate declared participant directories and report unknown writers."""
    exchange_root = Path(config["exchange"]["root"])
    root = exchange_root / "v2" / "publications"
    if _symlinked_parent(root, exchange_root):
        _error(config, "v2/publications", "PublicationRootEscapesExchange")
        return []
    try:
        children = list(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        _error(config, "v2/publications", "UnreadablePublicationRoot")
        return []
    participants = set(config["exchange"]["participants"])
    for child in children:
        if child.name not in participants:
            _error(config, f"publications/{child.name}", "UnknownParticipant")
    files: list[tuple[str, str]] = []
    for machine in config["exchange"]["participants"]:
        folder = root / machine
        if folder.is_symlink() or (folder.exists() and not _inside_real(folder, root)):
            _error(config, f"publications/{machine}", "InvalidParticipantDirectory")
            continue
        if not folder.is_dir():
            continue
        try:
            entries = list(folder.iterdir())
        except OSError:
            _error(config, f"publications/{machine}", "UnreadableParticipantDirectory")
            continue
        files.extend((machine, f"v2/publications/{machine}/{entry.name}")
                     for entry in entries if entry.suffix == ".json")
    return files


def _symlinked_parent(path: Path, root: Path) -> bool:
    """Detect symlinked exchange components that could redirect a publication."""
    relative = path.relative_to(root)
    current = root
    for component in relative.parts:
        current /= component
        if current.is_symlink() or (current.exists() and not _inside_real(current, root)):
            return True
    return False


def read_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every verified packet, retaining cached records through iCloud gaps."""
    validate_config(config)
    baseline = _read_baseline_cached(config)
    records = _read_cached_packets(config, baseline["snapshotId"])
    exchange_root = Path(config["exchange"]["root"])
    with SharedFiles(exchange_root) as shared:
        for machine, relative in _publication_files(config):
            location = relative.removeprefix("v2/")
            path_name = Path(relative).name
            try:
                raw_bytes = shared.read(relative, max_bytes=MAX_PACKET_BYTES)
                if raw_bytes is None:
                    raise FileNotFoundError(relative)
                raw = json.loads(raw_bytes.decode("utf-8"))
                packet = validate_packet(raw, machine, baseline["snapshotId"])
                if Path(path_name).stem != packet["id"]:
                    raise ValueError("publication filename does not match packet id")
                if not _project_is_allowed(packet["payload"], config):
                    raise ValueError("publication project is not allowed")
                records[packet["id"]] = packet
                _cache_packet(config, packet)
                _clear_error(config, location)
                _clear_error(config, f"cache/{packet['id']}.json")
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
                _error(config, location, "InvalidPublication")
    return [records[key] for key in sorted(records)]


def _publication_payload(config: dict[str, Any], job: dict[str, Any], result: dict[str, Any], baseline_id: str) -> dict[str, Any]:
    """Build a privacy-bounded accepted payload from a submitted contribution."""
    if result.get("status") != "submitted" or not isinstance(result.get("contribution"), dict):
        raise ValueError("submitted result requires contribution")
    contribution = result["contribution"]
    claims, evidence = contribution.get("claims"), contribution.get("evidence")
    if (not isinstance(claims, list) or not 1 <= len(claims) <= 5
            or not isinstance(evidence, list) or not 1 <= len(evidence) <= 20):
        raise ValueError("contribution exceeds the five-claim limit")
    if not valid_evidence_roles(claims, evidence):
        raise ValueError("assistant evidence requires historical lessons")
    shared_evidence = _shared_evidence(evidence, config["machineId"])
    evidence_by_id = {item["id"]: item for item in shared_evidence}
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("evidenceId") not in evidence_by_id or claim.get("quote") != evidence_by_id[claim["evidenceId"]]["text"]:
            raise ValueError("contribution claim is not an exact evidence quote")
    revisions = contribution.get("topicRevisions")
    if revisions is not None:
        from revision_contract import validate_topic_revisions
        revisions = validate_topic_revisions(revisions, claims, job.get("projectId"))
    basis = job.get("basisRecordIds", [])
    if not isinstance(basis, list) or any(not isinstance(item, str) for item in basis):
        raise ValueError("job basisRecordIds must be a string list")
    return {"version": 2, "baselineId": baseline_id, "machineId": config["machineId"],
            "projectId": job["projectId"], "projectLabel": job["projectLabel"],
            "createdAt": job["createdAt"], "originJobHash": digest(str(job.get("id", ""))),
            "repoIdentity": job.get("repoIdentity"), "basisRecordIds": list(basis),
            "claims": claims, "evidence": shared_evidence,
            **({"topicRevisions": revisions} if revisions is not None else {}),
            "review": {"status": "accepted", "model": str(config.get("model", ""))}}


def _project_is_allowed(payload: dict[str, Any], config: dict[str, Any]) -> bool:
    """Apply the existing publisher-side project ownership rules to v2 records."""
    try:
        from exchange import project_allowed
        return bool(project_allowed(payload, config))
    except (ImportError, KeyError, TypeError, ValueError):
        return False


def _shared_evidence(items: list[Any], machine: str) -> list[dict[str, Any]]:
    """Strip private locators and retain only exact quote metadata."""
    shared: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("kind") not in ("user", "artifact", "assistant"):
            raise ValueError("assistant evidence cannot be published")
        text, source_hash, locator = item.get("text"), item.get("sha256"), item.get("locator")
        if not isinstance(text, str) or not isinstance(locator, str) or not locator:
            raise ValueError("evidence quote or locator is invalid")
        if digest(text) != source_hash:
            raise ValueError("evidence quote hash mismatch")
        shared.append({"id": str(item.get("id", "")), "kind": item["kind"], "text": text,
                       "sha256": source_hash, "originalSha256": item.get("originalSha256", source_hash),
                       "observedAt": str(item.get("observedAt", "")),
                       "locator": f"knowledge-evidence://{machine}/{digest(locator)}"})
    return shared


def publish_record(config: dict[str, Any], job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Publish one reviewed record from any declared machine, idempotently."""
    validate_config(config)
    from exchange import require_legacy_importer
    require_legacy_importer(config, job)
    from semantic_scope import require_publication
    require_publication(config, job, result)
    if not config.get("publishEnabled"):
        raise ValueError("publication is disabled on this machine")
    baseline = _read_baseline_cached(config)
    payload = _publication_payload(config, job, result, baseline["snapshotId"])
    if not _project_is_allowed(payload, config):
        raise ValueError("publication project is not allowed")
    packet = {"id": digest(canonical(payload)), "payload": payload}
    validate_packet(packet, config["machineId"], baseline["snapshotId"])
    encoded = (canonical(packet) + "\n").encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("publication packet exceeds 128 KiB")
    root = Path(config["exchange"]["root"])
    relative = f"v2/publications/{config['machineId']}/{packet['id']}.json"
    with SharedFiles(root) as exchange:
        exchange.update(relative, encoded, None, packet["id"][:32], max_bytes=MAX_PACKET_BYTES)
    response = {key: value for key, value in result.items() if key != "contribution"} | {
        "status": "published", "publicationId": packet["id"], "publishedPageIds": result.get("publishedPageIds", [])}
    if "topicRevisions" in payload:
        response["contribution"] = {"topicRevisions": payload["topicRevisions"]}
    return response


def replica_status(config: dict[str, Any]) -> dict[str, Any]:
    """Return the last local replica status without reading or mutating shared data."""
    validate_config(config)
    status_path = Path(config["stateDir"]) / STATUS_PATH
    try:
        value = local_load_json(status_path, {})
    except (OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("digest", None)
    value.setdefault("count", 0)
    value.setdefault("recordIds", [])
    value.setdefault("conflicts", [])
    value["errors"] = _error_records(config)
    current = Path(config["stateDir"]) / "replica" / "current"
    value["current"] = current.is_symlink() and current.exists()
    value["generationRoot"] = None
    if value["current"]:
        try:
            value["generationRoot"] = str(current.resolve())
        except OSError:
            value["generationRoot"] = None
    return value
