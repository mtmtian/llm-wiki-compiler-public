"""Durable state for incremental consolidation of one project conversation.

The state is private to a host and keyed by both project and native session.  A
cursor advances only after queue finalisation, while the evidence retained here
is a bounded convenience window; immutable batch audits remain the history.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import digest, load_json, save_json
from queue_wire import job_bytes

UTC = dt.timezone.utc
STATE_VERSION = 1
# The evidence window is measured like the job byte limit (UTF-8 JSON, wire escaping). Counting characters
# let 40,000 CJK characters plus metadata exceed a whole 120,000-byte job, so every later turn of a long
# session was rejected as JobTooLarge. A third of the default job leaves room for the new turn.
MAX_EVIDENCE_BYTES = 40_000
MAX_CURSOR = 1_000
MAX_BATCHES = 1_000


def _timestamp(value: Any = None) -> str:
    """Return a canonical UTC timestamp, falling back to the current clock."""
    if isinstance(value, dt.datetime):
        parsed = value
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return parsed.isoformat()
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            return parsed.isoformat()
        except ValueError:
            pass
    return dt.datetime.now(UTC).isoformat()


def state_path(config: dict[str, Any], project_id: str, session_id: str) -> Path:
    """Resolve an isolated state filename without trusting user identifiers."""
    key = digest(str(project_id) + "\0" + str(session_id))
    return Path(config["stateDir"]) / "session-state" / (key + ".json")


@contextmanager
def _session_lock(config: dict[str, Any], project_id: str, session_id: str, exclusive: bool):
    """Serialise read-modify-write operations across hook and worker processes."""
    path = state_path(config, project_id, session_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _empty(project_id: str, session_id: str) -> dict[str, Any]:
    return {"version": STATE_VERSION, "projectId": project_id, "sessionId": session_id,
            "revision": 0, "summary": "", "topicPageIds": [], "evidence": [],
            "cursor": [], "committedBatches": [], "pending": []}


def load_session(config: dict[str, Any], project_id: str, session_id: str) -> dict[str, Any]:
    """Load and minimally normalise state; malformed state raises before new work."""
    path = state_path(config, project_id, session_id)
    value = load_json(path, {})
    if not path.exists():
        return _empty(project_id, session_id)
    _validate_state(value, project_id, session_id)
    state = {**_empty(project_id, session_id), **value}
    state["revision"] = int(state["revision"])
    state["topicPageIds"] = list(state.get("topicPageIds", []))
    state["cursor"] = list(state.get("cursor", []))[-MAX_CURSOR:]
    state["committedBatches"] = list(state.get("committedBatches", []))[-MAX_BATCHES:]
    state["evidence"] = list(state.get("evidence", []))
    state["pending"] = list(state.get("pending", []))
    return _trim_evidence(state)


def _validate_state(value: Any, project_id: str, session_id: str) -> None:
    """Reject corrupt durable fields instead of silently forgetting decisions."""
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        raise ValueError("invalid session state version")
    if value.get("projectId") != project_id or value.get("sessionId") != session_id:
        raise ValueError("invalid session state identity")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("invalid session state revision")
    if "summary" in value and not isinstance(value["summary"], str):
        raise ValueError("invalid session state summary")
    for field in ("topicPageIds", "cursor", "committedBatches", "evidence", "pending"):
        if field in value and not isinstance(value[field], list):
            raise ValueError("invalid session state " + field)
    for field in ("topicPageIds", "cursor", "committedBatches"):
        if any(not isinstance(item, str) for item in value.get(field, [])):
            raise ValueError("invalid session state " + field + " item")
    if any(not isinstance(item, dict) for item in value.get("evidence", [])):
        raise ValueError("invalid session state evidence item")
    for item in value.get("pending", []):
        if not isinstance(item.get("id"), str) or ("queuedAt" in item and not isinstance(item["queuedAt"], str)):
            raise ValueError("invalid session state pending item")
        if "queueFile" in item and not isinstance(item["queueFile"], str):
            raise ValueError("invalid session state pending queue file")
        if "explicit" in item and not isinstance(item["explicit"], bool):
            raise ValueError("invalid session state pending flag")
    for field in ("firstQueuedAt", "lastQueuedAt"):
        if field in value and not isinstance(value[field], str):
            raise ValueError("invalid session state " + field)
    if "explicit" in value and not isinstance(value["explicit"], bool):
        raise ValueError("invalid session state explicit flag")


def is_explicit_consolidation(job: dict[str, Any]) -> bool:
    """Recognise an explicit user request without treating ordinary turns as one."""
    if any(bool(job.get(key)) for key in ("requestConsolidation", "request_consolidation",
                                          "explicitConsolidation", "explicit_consolidation",
                                          "consolidationRequested", "consolidate")):
        return True
    prompt = str(job.get("prompt", ""))
    return bool(re.search(r"整理(?:会话|主题|页面|知识)|(?:总结|归纳|收敛).*(?:会话|主题|知识)|consolidat(?:e|ion)|summari[sz]e", prompt, re.I))


def _trim_evidence(state: dict[str, Any]) -> dict[str, Any]:
    """Keep newest original evidence under the convenience-window byte budget."""
    evidence = list(state.get("evidence", []))
    total = sum(job_bytes(item) for item in evidence)
    while evidence and total > MAX_EVIDENCE_BYTES:
        total -= job_bytes(evidence.pop(0))
    state["evidence"] = evidence
    return state


def context_for_job(config: dict[str, Any], project_id: str, session_id: str) -> dict[str, Any]:
    """Build model context from prior state; this is context, never a new citation."""
    with _session_lock(config, project_id, session_id, exclusive=False):
        state = load_session(config, project_id, session_id)
    return {"version": STATE_VERSION, "revision": state["revision"],
            "summary": state["summary"], "topicPageIds": state["topicPageIds"],
            "evidence": state["evidence"]}


def record_pending(config: dict[str, Any], job: dict[str, Any], queued_at: Any = None) -> dict[str, Any]:
    """Record an unfinalised source and return its scheduling snapshot."""
    project, session, identifier = job.get("projectId"), job.get("sessionId"), job.get("id")
    if not project or not session or not identifier:
        return {}
    with _session_lock(config, str(project), str(session), exclusive=True):
        state = load_session(config, str(project), str(session))
        _add_pending(state, job, queued_at)
        save_json(state_path(config, str(project), str(session)), _trim_evidence(state))
        return state


def _refresh_window(state: dict[str, Any]) -> None:
    """Recompute scheduling timestamps from the surviving pending entries."""
    pending = sorted(state["pending"], key=lambda item: item.get("queuedAt", ""))
    state["pending"] = pending
    if pending:
        state["firstQueuedAt"] = pending[0].get("queuedAt")
        state["lastQueuedAt"] = pending[-1].get("queuedAt")
        state["explicit"] = any(bool(item.get("explicit")) for item in pending)
    else:
        state.pop("firstQueuedAt", None)
        state.pop("lastQueuedAt", None)
        state.pop("explicit", None)


def _add_pending(state: dict[str, Any], job: dict[str, Any], queued_at: Any = None) -> None:
    """Append one source to an already locked state object."""
    identifier = str(job.get("id", ""))
    if not identifier or any(str(item.get("id")) == identifier for item in state["pending"]):
        return
    stamp = _timestamp(queued_at or job.get("queuedAt") or job.get("createdAt"))
    entry = {"id": identifier, "queuedAt": stamp, "explicit": is_explicit_consolidation(job)}
    if isinstance(job.get("queueFile"), str) and job["queueFile"]:
        entry["queueFile"] = job["queueFile"]
    state["pending"].append(entry)
    _refresh_window(state)


def persist_queued_job(config: dict[str, Any], job: dict[str, Any], queued: Path) -> dict[str, Any]:
    """Write the queue source first, then its checkpoint under one session lock."""
    project, session = job.get("projectId"), job.get("sessionId")
    if not project or not session:
        save_json(queued, job)
        return {}
    with _session_lock(config, str(project), str(session), exclusive=True):
        state = load_session(config, str(project), str(session))
        stamp = _timestamp()
        job["queueFile"] = queued.name
        first = state.get("firstQueuedAt") or stamp
        job["sessionSchedule"] = {"version": 1, "firstQueuedAt": first,
                                   "lastQueuedAt": stamp,
                                   "explicit": is_explicit_consolidation(job)}
        save_json(queued, job)
        _add_pending(state, job, stamp)
        save_json(state_path(config, str(project), str(session)), _trim_evidence(state))
        return state


def reconcile_queue_pending(config: dict[str, Any]) -> int:
    """Recover state after a queue write succeeded before its checkpoint write."""
    queue = Path(config["stateDir"]) / "queue"
    recovered = 0
    for path in sorted(queue.glob("*.json")):
        try:
            job = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(job, dict) or not job.get("id") or not job.get("projectId") or not job.get("sessionId"):
            continue
        if job.get("reviewRetryOf") or ("sessionSchedule" not in job and "sessionContext" not in job):
            continue
        if (Path(config["stateDir"]) / "completed" / (str(job["id"]) + ".json")).exists():
            continue
        job["queueFile"] = path.name
        with _session_lock(config, str(job["projectId"]), str(job["sessionId"]), exclusive=True):
            state = load_session(config, str(job["projectId"]), str(job["sessionId"]))
            before = len(state["pending"])
            _add_pending(state, job, _schedule_time(job))
            if len(state["pending"]) > before:
                save_json(state_path(config, str(job["projectId"]), str(job["sessionId"])), _trim_evidence(state))
                recovered += 1
    _reconcile_terminal_pending(config)
    return recovered


def _schedule_time(job: dict[str, Any]) -> str | None:
    schedule = job.get("sessionSchedule")
    if isinstance(schedule, dict) and isinstance(schedule.get("lastQueuedAt"), str):
        return schedule["lastQueuedAt"]
    return job.get("createdAt") if isinstance(job.get("createdAt"), str) else None


def _reconcile_terminal_pending(config: dict[str, Any]) -> None:
    """Release terminal or impossible queue markers without racing a new enqueue."""
    root = Path(config["stateDir"])
    for path in sorted((root / "session-state").glob("*.json")):
        value = load_json(path)
        if not isinstance(value, dict) or not value.get("projectId") or not value.get("sessionId"):
            continue
        project, session = str(value["projectId"]), str(value["sessionId"])
        with _session_lock(config, project, session, exclusive=True):
            state = load_session(config, project, session)
            retained = []
            for item in state["pending"]:
                identifier, queue_file = str(item.get("id", "")), item.get("queueFile")
                names = [identifier + ".json"]
                if isinstance(queue_file, str) and queue_file:
                    names.append(queue_file)
                receipt = any((root / folder / name).exists()
                              for folder in ("completed", "failed") for name in names)
                queue_exists = isinstance(queue_file, str) and (root / "queue" / queue_file).is_file()
                active = _active_audit(root, identifier, queue_file)
                if not receipt and (queue_exists or active or not queue_file):
                    retained.append(item)
            if len(retained) != len(state["pending"]):
                state["pending"] = retained
                _refresh_window(state)
                save_json(state_path(config, project, session), _trim_evidence(state))


def _active_audit(root: Path, identifier: str, queue_file: Any) -> bool:
    """Keep a marker while an unfinished durable batch still owns its source."""
    for path in (root / "batches").glob("*.json"):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(audit, dict) or audit.get("status") not in (
                "claimed", "retry", "sync-retry", "result-ready", "finalize-retry"):
            continue
        if queue_file in audit.get("queueFiles", []) or identifier in audit.get("sourceJobIds", []):
            return True
        job = audit.get("job", {})
        if isinstance(job, dict) and identifier in job.get("sourceJobIds", []):
            return True
    return False


def discard_pending(config: dict[str, Any], job: dict[str, Any] | None = None,
                    identifiers: list[str] | None = None) -> dict[str, Any]:
    """Remove terminal sources from the scheduling window without advancing a cursor."""
    project = job.get("projectId") if job else None
    session = job.get("sessionId") if job else None
    values = identifiers or (job.get("sourceJobIds", [job.get("id")]) if job else [])
    if not project or not session:
        return {}
    wanted = {str(value) for value in values if value}
    with _session_lock(config, str(project), str(session), exclusive=True):
        state = load_session(config, str(project), str(session))
        retained = [item for item in state["pending"] if str(item.get("id")) not in wanted]
        if len(retained) == len(state["pending"]):
            return state
        state["pending"] = retained
        _refresh_window(state)
        save_json(state_path(config, str(project), str(session)), _trim_evidence(state))
        return state


def discard_pending_queue_file(config: dict[str, Any], queue_file: str) -> int:
    """Release pending entries indexed by a queue filename after parse failure."""
    if not isinstance(queue_file, str) or not queue_file:
        return 0
    released = 0
    root = Path(config["stateDir"]) / "session-state"
    for path in sorted(root.glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(value, dict) or not value.get("projectId") or not value.get("sessionId"):
            continue
        ids = [item.get("id") for item in value.get("pending", [])
               if isinstance(item, dict) and item.get("queueFile") == queue_file]
        if ids:
            discard_pending(config, value, [str(item) for item in ids])
            released += len(ids)
    return released


def _append_evidence(state: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    seen = {(item.get("id"), item.get("sha256"), item.get("kind")) for item in state["evidence"]}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        identity = (item.get("id"), item.get("sha256"), item.get("kind"))
        if identity not in seen:
            state["evidence"].append(item)
            seen.add(identity)


def commit_batch(config: dict[str, Any], job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Advance the session cursor once for a successfully finalised batch."""
    project, session = job.get("projectId"), job.get("sessionId")
    if not project or not session:
        return {}
    with _session_lock(config, str(project), str(session), exclusive=True):
        state = load_session(config, str(project), str(session))
        identifiers = [str(v) for v in job.get("sourceJobIds", [job.get("id")]) if v]
        batch_id = str(job.get("id", ""))
        duplicate_batch = bool(batch_id and batch_id in state["committedBatches"])
        unseen = [] if duplicate_batch else [value for value in identifiers if value not in state["cursor"]]
        if unseen:
            _append_evidence(state, list(job.get("evidence", [])))
            state["cursor"] = (state["cursor"] + unseen)[-MAX_CURSOR:]
            state["revision"] += 1
            if batch_id:
                state["committedBatches"] = (state["committedBatches"] + [batch_id])[-MAX_BATCHES:]
        memory = result.get("sessionMemory") if isinstance(result, dict) else None
        if unseen and isinstance(memory, dict):
            if isinstance(memory.get("summary"), str):
                state["summary"] = memory["summary"]
            if isinstance(memory.get("topicPageIds"), list):
                state["topicPageIds"] = [str(v) for v in memory["topicPageIds"] if isinstance(v, str)]
        state["pending"] = [item for item in state["pending"] if str(item.get("id")) not in identifiers]
        _refresh_window(state)
        save_json(state_path(config, str(project), str(session)), _trim_evidence(state))
        return state
