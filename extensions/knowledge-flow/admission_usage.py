"""Calculate bounded runnable and waiting intake from durable local records.

Capacity-deferred batches retain their queue files for recovery. A matching
frozen source hash lets those files count as waiting instead of occupying a
runnable slot, without transferring ownership or weakening malformed state.
"""

import json
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import digest, load_json

SCHEDULING_FIELDS = frozenset({"attempts", "nextAttemptAt", "notBefore", "queueFile", "sessionSchedule"})


@contextmanager
def intake_lock(config: dict[str, Any]):
    """Serialize short runnable/wait admission checks and their state writes."""
    path = Path(config["stateDir"]) / "intake.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def source_hash(job: dict[str, Any]) -> str:
    """Hash immutable queue input while ignoring queue-owned schedule markers."""
    frozen = {key: value for key, value in job.items() if key not in SCHEDULING_FIELDS}
    return digest(json.dumps(frozen, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _load_queue(state: Path) -> tuple[list[Path], dict[str, dict[str, Any] | None]]:
    """Read each queue filename once; unreadable files still occupy runnable capacity."""
    paths = sorted((state / "queue").glob("*.json"))
    values = {}
    for path in paths:
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            value = None
        values[path.name] = value if isinstance(value, dict) else None
    return paths, values


def _valid_deferred_audit(path: Path, audit: Any,
                          queue: dict[str, dict[str, Any] | None]) -> set[str] | None:
    """Verify one deferred batch against every retained source and claim hash."""
    if not isinstance(audit, dict) or audit.get("status") != "capacity-deferred":
        return None
    names, job, hashes = audit.get("queueFiles"), audit.get("job"), audit.get("queueSourceHashes")
    if (audit.get("batchId") != path.stem or not isinstance(names, list) or not names
            or any(not isinstance(name, str) or Path(name).name != name for name in names)
            or len(set(names)) != len(names) or not isinstance(job, dict)
            or job.get("id") != path.stem or job.get("sourceQueueFiles") != names
            or not isinstance(job.get("sourceJobIds"), list) or not isinstance(hashes, dict)
            or set(hashes) != set(names)):
        return None
    sources = [queue.get(name) for name in names]
    if (any(not isinstance(source, dict) for source in sources)
            or [source.get("id") for source in sources] != job["sourceJobIds"]):
        return None
    return set(names) if all(hashes.get(name) == source_hash(source)
                             for name, source in zip(names, sources)) else None


def _deferred_claims(state: Path, queue: dict[str, dict[str, Any] | None]) -> set[str]:
    """Return source names backed by one complete, byte-stable deferred audit."""
    candidates = []
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        names = _valid_deferred_audit(path, audit, queue)
        if names:
            candidates.append(names)
    occurrences = {}
    for names in candidates:
        for name in names:
            occurrences[name] = occurrences.get(name, 0) + 1
    return {name for names in candidates for name in names
            if all(occurrences.get(item) == 1 for item in names)}


def _capacity_pending_count(state: Path, queue: dict[str, dict[str, Any] | None]) -> int:
    """Count capacity waits conservatively, excluding a verified duplicate transfer."""
    count = 0
    for path in sorted((state / "capture-pending").glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            count += 1
            continue
        if not isinstance(value, dict):
            count += 1
            continue
        if value.get("kind") != "capacity":
            if value.get("kind") is None and value.get("event") and value.get("record"):
                continue
            count += 1
            continue
        job = value.get("job")
        identifier = value.get("id")
        name = value.get("queueFile") or (str(identifier) + ".json")
        queued = queue.get(name)
        if (identifier == path.stem and isinstance(job, dict) and job.get("id") == path.stem
                and value.get("jobHash") == _job_hash(job) and isinstance(queued, dict)
                and source_hash(queued) == source_hash(job)):
            continue
        count += 1
    return count


def _job_hash(job: dict[str, Any]) -> str:
    """Match capture_retry's exact snapshot digest without importing its writer."""
    return digest(json.dumps(job, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def admission_usage(state: Path) -> dict[str, Any]:
    """Return runnable, waiting, and total admitted counts from durable state."""
    state = Path(state)
    queue_paths, queue = _load_queue(state)
    deferred = _deferred_claims(state, queue)
    runnable = len(queue_paths) - len(deferred)
    waiting = len(deferred) + _capacity_pending_count(state, queue)
    return {"runnable": runnable, "waiting": waiting, "total": runnable + waiting,
            "deferredFiles": deferred}


def can_admit_runnable(state: Path, max_jobs: int, count: int = 1) -> bool:
    """Check a new source against both the runnable cap and total envelope."""
    usage = admission_usage(state)
    maximum = max(0, int(max_jobs))
    return usage["runnable"] + count <= maximum and usage["total"] + count <= 2 * maximum


def runnable_capacity_reason(state: Path, max_jobs: int) -> str | None:
    """Explain why a new runnable source cannot fit under both admission limits."""
    usage = admission_usage(state)
    maximum = max(0, int(max_jobs))
    if usage["runnable"] >= maximum:
        return "runtime-queue-full"
    return "admission-envelope-full" if usage["total"] >= 2 * maximum else None


def can_admit_wait(state: Path, max_jobs: int) -> bool:
    """Check a new capacity wait against its reservation and total envelope."""
    usage = admission_usage(state)
    maximum = max(0, int(max_jobs))
    return usage["waiting"] < maximum and usage["total"] < 2 * maximum


def can_restore_pending(state: Path, max_jobs: int) -> bool:
    """Check a capacity-pending to queue transfer without charging a second admission."""
    usage = admission_usage(state)
    return usage["runnable"] + 1 <= max(0, int(max_jobs))


def can_activate_claimed(state: Path, max_jobs: int, queue_files: list[str]) -> bool:
    """Check a deferred-to-runnable transition without changing its source files."""
    usage = admission_usage(state)
    activation = sum(name in usage["deferredFiles"] for name in queue_files)
    return usage["runnable"] + activation <= max(0, int(max_jobs))
