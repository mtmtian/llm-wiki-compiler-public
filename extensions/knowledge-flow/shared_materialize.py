"""Reconcile the authorized machine's shared Obsidian projection.

Publication records remain independently writable by every participant. Only
the fixed or acknowledged current owner maintains shared pages; its private hash
manifest distinguishes generated files from human edits. A durable pending
plan permits retries after interruption, and replaced files remain recoverable.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import load_json, save_json
from replica_records import baseline_path_allowed, safe_relative
from replica_integrity import read_verified_generation
from shared_files import SharedFiles
from shared_retirement import desired_retirement, retired_baseline

MANIFEST = "shared-materialization.json"
PENDING = "shared-materialization-pending.json"
PROMOTED_DIRS = ("sources", "wiki/concepts")
NAVIGATION = ("wiki/MOC.md", "wiki/index.md")
RECORD_PAGE = re.compile(r"(?:^|-)record-[a-f0-9]{32}-[0-9]+\.md$")


def _hash(content: bytes | None) -> str | None:
    """Hash exact bytes; absence is different from an empty file."""
    return None if content is None else hashlib.sha256(content).hexdigest()


def _baseline_topic_page(relative: str, baseline: dict[str, bytes]) -> bool:
    """Identify baseline concept pages eligible for reviewed topic reuse."""
    return (relative in baseline and relative.startswith("wiki/concepts/")
            and relative.endswith(".md"))


def _scope(config: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Bind ownership to this machine, shared root and immutable baseline."""
    return {"version": 1, "machineId": config["machineId"],
            "sharedWikiRoot": str(Path(config["sharedWikiRoot"]).absolute()),
            "baselineId": baseline["snapshotId"]}


def _load(path: Path, scope: dict[str, Any]) -> dict[str, Any] | None:
    """Reject corrupt or foreign ownership metadata instead of adopting it."""
    if path.is_symlink():
        raise ValueError("shared materialization metadata must not be a symlink")
    value = load_json(path, None)
    if value is not None and (not isinstance(value, dict) or value.get("scope") != scope):
        raise ValueError("shared materialization ownership does not match this deployment")
    return value


def _save(path: Path, value: dict[str, Any]) -> None:
    """Persist the recovery plan before any shared file can be moved."""
    save_json(path, value)
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _candidate_files(generation: Path, baseline: dict[str, bytes]) -> dict[str, bytes]:
    """Use a verified snapshot so missing cache files cannot mean retraction."""
    projection = read_verified_generation(generation).projection
    return {relative: content for relative, content in projection.items()
            if relative in NAVIGATION
            or _baseline_topic_page(relative, baseline) and content != baseline[relative]
            or relative not in baseline and baseline_path_allowed(relative)}


def _managed_path(relative: str, baseline: dict[str, bytes]) -> bool:
    """Ownership permits only navigation, topic reuse, and new derived files."""
    safe_relative(relative)
    return (relative in NAVIGATION or _baseline_topic_page(relative, baseline)
            or (relative not in baseline and relative.endswith(".md")
                and any(relative.startswith(directory + "/") for directory in PROMOTED_DIRS)))


def _prior_files(manifest: dict[str, Any] | None, baseline: dict[str, bytes]) -> dict[str, str]:
    """Validate every persisted owned path before planning changes."""
    files = manifest.get("files") if manifest else {}
    if not isinstance(files, dict):
        raise ValueError("invalid shared materialization file manifest")
    for relative, value in files.items():
        if (not isinstance(relative, str) or not _managed_path(relative, baseline)
                or not isinstance(value, str) or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)):
            raise ValueError("invalid shared materialization ownership entry")
    return files


def _plan(files: SharedFiles, desired: dict[str, bytes], prior: dict[str, str],
          baseline: dict[str, bytes], scope: dict[str, Any],
          retirement: tuple[set[str], set[str]] = (frozenset(), frozenset())) -> dict[str, Any]:
    """Preflight the complete projection before changing any shared file."""
    owned = dict(desired)
    retired, previously_retired = retirement
    for relative in set(prior) | previously_retired:
        if relative not in owned and relative not in retired and _baseline_topic_page(relative, baseline):
            owned[relative] = baseline[relative]
    changes = {}
    for relative in sorted(set(owned) | set(prior) | retired):
        current, wanted = files.read(relative), owned.get(relative)
        baseline_topic = _baseline_topic_page(relative, baseline)
        if (relative not in prior and current is not None
                and current != baseline.get(relative)):
            if baseline_topic:
                raise ValueError(f"shared materialization conflict: {relative}")
            raise ValueError(f"untracked shared content requires migration: {relative}")
        if current == wanted:
            continue
        expected = prior.get(relative)
        if (relative in NAVIGATION or baseline_topic) and relative not in prior:
            expected = _hash(baseline.get(relative))
        if relative in previously_retired:
            expected = None
        if _hash(current) != expected:
            raise ValueError(f"shared materialization conflict: {relative}")
        changes[relative] = {"before": None if current is None else current.decode("utf-8"),
                             "after": None if wanted is None else wanted.decode("utf-8")}
    return {"scope": scope, "transaction": uuid.uuid4().hex, "changes": changes,
            "files": {relative: _hash(content) for relative, content in owned.items()},
            "retiredBaseline": sorted(retired)}


def _pending_changes(plan: dict[str, Any], baseline: dict[str, bytes]) -> dict[str, Any]:
    """Validate durable recovery data before it can address the shared vault."""
    _prior_files(plan, baseline)
    retired_baseline(plan.get("retiredBaseline"), baseline)
    changes = plan.get("changes")
    if not isinstance(changes, dict) or not isinstance(plan.get("transaction"), str):
        raise ValueError("invalid shared materialization recovery plan")
    for relative, change in changes.items():
        if not isinstance(relative, str) or not _managed_path(relative, baseline) or not isinstance(change, dict):
            raise ValueError("invalid shared materialization recovery path")
        if set(change) != {"before", "after"} or any(value is not None and not isinstance(value, str) for value in change.values()):
            raise ValueError("invalid shared materialization recovery content")
        after = change["after"]
        if _hash(None if after is None else after.encode("utf-8")) != plan["files"].get(relative):
            raise ValueError("shared materialization recovery hash mismatch")
    return changes


def _apply(files: SharedFiles, state: Path, plan: dict[str, Any], baseline: dict[str, bytes]) -> dict[str, Any]:
    """Finish one durable plan; every update is restartable and preserves edits."""
    changes = _pending_changes(plan, baseline)
    for relative, change in changes.items():
        current = files.read(relative)
        allowed = [None if value is None else value.encode("utf-8") for value in change.values()]
        if current is not None and current not in allowed:
            raise ValueError(f"shared materialization conflict: {relative}")
    backups = []
    for relative, change in changes.items():
        before, after = (None if change[key] is None else change[key].encode("utf-8") for key in ("before", "after"))
        backup = files.update(relative, after, before, plan["transaction"])
        if backup:
            backups.append(backup)
    _save(state / MANIFEST, {"scope": plan["scope"], "files": plan["files"],
                            "retiredBaseline": plan.get("retiredBaseline", [])})
    (state / PENDING).unlink(missing_ok=True)
    return {"written": sum(change["after"] is not None for change in changes.values()),
            "removed": sum(change["after"] is None for change in changes.values()), "backups": backups}


def _reconcile(config: dict[str, Any], generation: Path, baseline: dict[str, Any], state: Path) -> dict[str, Any]:
    """Recover interrupted work, then reconcile the latest verified generation."""
    scope = _scope(config, baseline)
    original = {item["path"]: item["text"].encode("utf-8") for item in baseline["files"]}
    desired = _candidate_files(generation, original)
    with SharedFiles(Path(config["sharedWikiRoot"])) as files:
        pending = _load(state / PENDING, scope)
        if pending:
            _apply(files, state, pending, original)
        manifest = _load(state / MANIFEST, scope)
        previous = _prior_files(manifest, original)
        prior_retired = retired_baseline((manifest or {}).get("retiredBaseline"), original)
        retired = desired_retirement(config, original, read_verified_generation(generation).projection)
        _check_untracked(Path(config["sharedWikiRoot"]), set(previous) | set(original))
        plan = _plan(files, desired, previous, original, scope, (retired, prior_retired))
        if not plan["changes"] and previous == plan["files"] and retired == prior_retired:
            return {"status": "current", "written": 0, "removed": 0, "backups": []}
        _save(state / PENDING, plan)
        return {"status": "current", **_apply(files, state, plan, original)}


def _check_untracked(shared: Path, known: set[str]) -> None:
    """Do not claim a clean projection while legacy unowned record pages remain."""
    for directory in PROMOTED_DIRS:
        for path in (shared / directory).rglob("*.md"):
            relative = path.relative_to(shared).as_posix()
            generated = RECORD_PAGE.search(path.name) or path.name.startswith("knowledge-flow-")
            if generated and relative not in known:
                raise ValueError(f"untracked shared publication requires migration: {relative}")


def _validate_expected_hashes(value: Any, baseline: dict[str, bytes]) -> dict[str, str]:
    """Validate a reviewed inventory of legacy files without accepting paths by guesswork."""
    if not isinstance(value, dict):
        raise ValueError("legacy migration expectedHashes must be an object")
    result: dict[str, str] = {}
    for relative, expected in value.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("legacy migration expectedHashes entry is invalid")
        safe_relative(relative)
        name = Path(relative).name
        legacy = relative in NAVIGATION or (
            relative not in baseline and
            ((relative.startswith("wiki/concepts/") and RECORD_PAGE.search(name) is not None)
             or (relative.startswith("sources/") and name.startswith("knowledge-flow-")
                 and name.endswith(".md"))))
        if not _managed_path(relative, baseline) or not legacy or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("legacy migration expectedHashes entry is invalid")
        result[relative] = expected
    return result


def _verify_expected_hashes(files: SharedFiles, expected: dict[str, str]) -> None:
    """Require every reviewed legacy path to still contain its exact original bytes."""
    for relative, expected_hash in expected.items():
        content = files.read(relative)
        if content is None or _hash(content) != expected_hash:
            raise ValueError(f"legacy migration hash mismatch: {relative}")


def _legacy_dry_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Describe a migration plan using paths and actions only, never page contents."""
    changes = plan["changes"]
    written = sorted(relative for relative, change in changes.items() if change["after"] is not None)
    removed = sorted(relative for relative, change in changes.items() if change["after"] is None)
    backups = sorted(relative for relative, change in changes.items() if change["before"] is not None)
    return {"status": "dry-run", "written": len(written), "removed": len(removed),
            "writePaths": written, "removePaths": removed, "backupPaths": backups}


@contextmanager
def _migration_lock(state: Path, dry_run: bool):
    """Reuse the materializer lock while keeping a dry run free of state writes."""
    lock_path = state / "shared-materialization.lock"
    if dry_run and not lock_path.exists():
        yield
        return
    if lock_path.is_symlink():
        raise ValueError("shared materialization lock must not be a symlink")
    if not dry_run:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("r" if dry_run else "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def migrate_legacy_projection(config: dict[str, Any], generation_root: str,
                              baseline: dict[str, Any], expectedHashes: dict[str, str],
                              dry_run: bool = True) -> dict[str, Any]:
    """Migrate reviewed legacy pages into one verified v2 projection transaction."""
    if _coordinated(config):
        raise ValueError('legacy migration cannot bypass coordinated shared ownership')
    designated = config.get("exchange", {}).get("materializerMachineId")
    if not designated:
        return {"status": "disabled"}
    if designated != config.get("machineId"):
        return {"status": "not-materializer", "machineId": designated}
    if not config.get("enabled", True) or not config.get("publishEnabled", False):
        return {"status": "paused"}
    original = {item["path"]: item["text"].encode("utf-8") for item in baseline["files"]}
    desired = _candidate_files(Path(generation_root), original)
    expected = _validate_expected_hashes(expectedHashes, original)
    state = Path(config["stateDir"])
    with _migration_lock(state, dry_run):
        pending = _load(state / PENDING, _scope(config, baseline))
        if pending:
            raise ValueError("legacy migration has a pending materialization")
        manifest = _load(state / MANIFEST, _scope(config, baseline))
        if manifest:
            raise ValueError("shared materialization ownership already exists; retry with promote_generation")
        with SharedFiles(Path(config["sharedWikiRoot"])) as files:
            _verify_expected_hashes(files, expected)
            _check_untracked(Path(config["sharedWikiRoot"]), set(original) | set(expected) | set(desired))
            plan = _plan(files, desired, expected, original, _scope(config, baseline))
            if dry_run:
                return _legacy_dry_summary(plan)
            _save(state / PENDING, plan)
            return {"status": "current", **_apply(files, state, plan, original)}


def promote_generation(config: dict[str, Any], generation_root: str, baseline: dict[str, Any]) -> dict[str, Any]:
    """Serialize legacy ownership or acknowledged cooperative ownership locally."""
    coordinated = _coordinated(config)
    designated = config["exchange"].get("materializerMachineId")
    if not designated:
        return {"status": "disabled"}
    if not coordinated and designated != config["machineId"]:
        return {"status": "not-materializer", "machineId": designated}
    if not config.get("enabled", True) or not config.get("publishEnabled", False):
        return {"status": "paused"}
    state = Path(config["stateDir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "shared-materialization.lock").open("a") as lock:
        # This lock serializes only this machine. Fixed identity or a durable
        # cooperative handoff selects the writer across the participants.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if coordinated:
            from writer_handoff import coordinate
            return coordinate(config, Path(generation_root), baseline)
        return _reconcile(config, Path(generation_root), baseline, state)


def _coordinated(config: dict[str, Any]) -> bool:
    """Prevent upgraded machines from falling back after coordination activation."""
    from writer_records import settings, BOOTSTRAP
    profile = settings(config)
    root = config.get('exchange', {}).get('root')
    guarded = (Path(config['stateDir']) / 'shared-writer-state.json').exists()
    activated = isinstance(root, str) and (Path(root) / BOOTSTRAP).exists()
    if profile is None and (guarded or activated):
        raise ValueError('shared writer coordination cannot be disabled after activation')
    return profile is not None
