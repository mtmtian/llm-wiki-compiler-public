"""Codex event adapter: scoped context and quick, evidence-based intake.

This adapter never blocks a user turn. A Stop event is only a candidate signal,
not proof of task completion. Private queue files allow the event worker to recover interrupted jobs without
re-reading unrelated conversation history.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from common import (config_from, digest, inside, is_excluded_artifact_path, load_json,
                    page_ids, read_operational_context, safe_text, save_json)
from hook_context import prepare_context
from current_decisions import current_decisions
from context_observation import (promote_prepared_context, record_stop_observation,
                                save_prepared_context, write_diagnostic)
from operational_context import operational_context
from read_routing import resolve_read
from routing import git_identity, resolve, resolve_repo_identity
from capture import capture_evidence_result, has_substantive_evidence
from host_automation import capture_automation_reason, job_automation_reason, prompt_automation_reason
from capture_retry import pending_path, record_capture_pending
from queue_worker import process_queue as drain_queue
from queue_worker import MAX_JOB_BYTES
from turn_routing import recover_turn_route
from queue_wire import EventTooLarge, checked_request, job_bytes
from session_schedule import enabled as session_enabled
from session_state import context_for_job, persist_queued_job


def now():
    """UTC timestamps make cross-machine evidence dates unambiguous."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def event_path(config, event):
    """Hash identifiers rather than trusting event strings as path components."""
    key = digest(str(event.get("session_id", "")) + ":" + str(event.get("turn_id", "")))
    return Path(config["stateDir"]) / "turns" / (key + ".json")


def session_path(config, session_id):
    """Return one private session record path."""
    return Path(config["stateDir"]) / "sessions" / (digest(str(session_id)) + ".json")


def read_context_event(event, config, project, reason, prompt, session):
    """Prepare a scoped Wiki packet and return the host hook payload."""
    allowed = page_ids(config, project) if project else []
    result = prepare_context(config, project, prompt, allowed, session.get("seen", {}), invoke,
                             operational_context(config, prompt))
    write_diagnostic(config, event, project, reason, result, prompt)
    decisions = unseen_decisions(config, project, session)
    save_prepared_context(config, event, session, project, prompt, result)
    text = decisions + result.get("context", "")
    if not text:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event.get("hook_event_name", "UserPromptSubmit"),
                                    "additionalContext": text}}


def unseen_decisions(config, project, session):
    """Return the project's decision digest once per session and again whenever it changes.

    Records the delivered fingerprint in ``session["decisionsSeen"]``; the caller
    persists that dict (``save_prepared_context`` writes the whole session).
    """
    if not project:
        return ""
    text, fingerprint = current_decisions(config, project)
    seen = session.get("decisionsSeen") if isinstance(session.get("decisionsSeen"), dict) else {}
    if not text or seen.get(project) == fingerprint:
        return ""
    session["decisionsSeen"] = {**seen, project: fingerprint}
    return text


def session_start_event(event, config):
    """Handle lifecycle signals before turn_id validation and restore only safe read context."""
    source = str(event.get("source", ""))
    path = session_path(config, event["session_id"])
    if source == "clear":
        path.unlink(missing_ok=True)
        return {}
    if source not in ("compact", "resume"):
        return {}
    session = load_json(path, {})
    if session.pop("decisionsSeen", None) is not None:
        # Compaction drops earlier injected text, so the next routed prompt restates the digest.
        save_json(path, session)
    project, prompt = session.get("readProjectId"), safe_text(session.get("readPrompt"), 12000)
    if not project or not prompt:
        return {}
    verified, reason = resolve_read(event["cwd"], prompt, project, config)
    if verified != project:
        save_json(path, {**session, "seen": {}, "preparedRead": None})
        write_diagnostic(config, event, None, reason,
                         {"status": "no-scope", "prepared": False, "complete": False}, prompt)
        return {}
    result = prepare_context(config, project, prompt, page_ids(config, project), {}, invoke,
                             operational_context(config, prompt))
    write_diagnostic(config, event, project, "session-" + source, result, prompt)
    session["preparedRead"] = {"turnId": "lifecycle-" + source, "projectId": project,
                                "seen": result.get("seen", {}), "text": result.get("context", ""),
                                "complete": bool(result.get("complete")),
                                "references": result.get("references", []),
                                "referencesTracked": bool(result.get("referencesTracked"))}
    session["seen"] = {}
    save_json(path, session)
    text = result.get("context", "")
    if not text:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def invoke(config, command, value, timeout):
    """Call the installed bounded worker; keep its diagnostics out of conversation context."""
    environment = os.environ.copy()
    for key in ("LLMWIKI_EMBEDDING_PROVIDER", "LLMWIKI_EMBEDDING_MODEL", "OLLAMA_EMBEDDINGS_HOST", "LLMWIKI_EMBED_STRICT"):
        if key in config.get("environment", {}):
            environment[key] = str(config["environment"][key])
    encoded = checked_request(config, command, value)
    result = subprocess.run([config["node"], config["worker"], command], env=environment,
                            input=encoded, text=False,
                            capture_output=True, timeout=timeout, check=True)
    return json.loads(result.stdout.decode("utf-8"))


def prompt_event(event, config):
    """Bind this turn and inject only new, relevant accepted knowledge."""
    prompt = safe_text(event.get("prompt"), 12000)
    session = session_path(config, event["session_id"])
    previous = load_json(session, {})
    read_project, read_reason = resolve_read(event["cwd"], prompt,
                                             previous.get("readProjectId") or previous.get("projectId"), config)
    if read_reason == "operational-question":
        previous = {}
        save_json(session, previous)
    project, reason = resolve(event["cwd"], prompt, previous.get("projectId"), config)
    record = {"projectId": project, "reason": reason, "readProjectId": read_project,
              "readReason": read_reason, "createdAt": now(), "sessionId": event.get("session_id"),
              "turnId": event.get("turn_id"), "cwd": event.get("cwd")}
    automation = prompt_automation_reason(event.get("session_id"), event.get("prompt"))
    if automation:
        record["hostAutomationReason"] = automation
    if isinstance(event.get("transcript_path"), str):
        record["transcriptPath"] = event["transcript_path"]
    if project:
        record["prompt"] = prompt
        identity = resolve_repo_identity(event["cwd"], prompt, project, config)
        record["repoIdentity"] = identity or (previous.get("repoIdentity") if previous.get("projectId") == project else None)
    save_json(event_path(config, event), record)
    if not project and not read_project:
        operation = operational_context(config, prompt)
        result = prepare_context(config, "", prompt, [], {}, invoke, operation)
        write_diagnostic(config, event, None, read_reason, result, prompt)
        text = result.get("context", "")
        if text:
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
        return {}
    session_value = {**previous, "projectId": project, "repoIdentity": record.get("repoIdentity"),
                     "readProjectId": read_project}
    save_json(session, session_value)
    return read_context_event(event, config, read_project, read_reason, prompt, session_value)


def artifact_evidence(text, cwd):
    """Read a few explicitly linked local report files, confined to the task workspace."""
    paths = re.findall(r"\]\(<?(/[^)\n>]+)>?\)", text)
    result = []
    for raw in dict.fromkeys(paths):
        path = Path(re.sub(r":\d+(?::\d+)?$", "", raw))
        if len(result) >= 3:
            break
        extensions = (".md", ".txt", ".json", ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go", ".swift", ".sql")
        if (path.suffix.lower() not in extensions or not inside(path, cwd)
                or is_excluded_artifact_path(path)):
            continue
        if re.search(r"(?:secret|credential|auth|token|password|\.env)", path.name, re.I):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 100000:
                continue
            content = safe_text(path.read_text(), 16000)
            result.append({"id": "artifact-" + str(len(result) + 1), "kind": "artifact",
                           "text": content, "locator": str(path), "sha256": digest(content), "observedAt": now()})
        except (OSError, UnicodeError):
            continue
    return result


def prepare_job(event, record, config, captured=None, artifact_snapshot=None):
    """Create bounded evidence; assistant text never proves implementation."""
    project = record["projectId"]
    prompt = record["prompt"]
    repo_identity = record.get("repoIdentity")
    if not repo_identity and project.startswith("repo-"):
        repo_identity = git_identity(event["cwd"])[1]
    project_config = config["projects"].get(project, {})
    project_label = project_config.get("label") or (
        str(repo_identity).rsplit("/", 1)[-1] if repo_identity and "/" in str(repo_identity) else project)
    assistant = safe_text(event.get("last_assistant_message"), 16000)
    source = {**event}
    if "transcript_path" not in source and record.get("transcriptPath"):
        source["transcript_path"] = record["transcriptPath"]
    captured = captured if captured is not None else capture_evidence_result(source, record, config)
    evidence = list(captured["evidence"])
    evidence += (list(artifact_snapshot) if artifact_snapshot is not None
                 else artifact_evidence(assistant, event["cwd"]))
    return {"id": event_path(config, event).stem, "projectId": project,
            "projectLabel": project_label,
            "sessionId": event["session_id"], "turnId": event["turn_id"], "cwd": event["cwd"],
            "createdAt": record.get("createdAt") or now(), "prompt": prompt, "lastAssistant": assistant,
            "evidence": evidence, "allowedPageIds": page_ids(config, project, topic_scope="project"),
            "captureStatus": captured["status"], "captureReason": captured.get("reason", ""),
            "intakeFilterReason": capture_automation_reason(event, record, captured),
            "repoIdentity": repo_identity}


def process_queue(config, limit=3):
    """Share one queue implementation across hooks, manual recovery, and event wakes."""
    return drain_queue(config, invoke, limit=limit)


def clear_capture_error(state, identifier):
    """Clear only this turn's capture notification after a successful retry."""
    (state / "capture-errors" / (identifier + ".json")).unlink(missing_ok=True)
    try:
        error = load_json(state / "last-error.json", {})
    except (OSError, ValueError, TypeError):
        return
    if isinstance(error, dict) and error.get("jobId") == identifier and error.get("status") == "error":
        (state / "last-error.json").unlink(missing_ok=True)


def record_capture_error(state, identifier, error_type, reason=""):
    """Persist a retryable intake error for maintenance and notification."""
    error = {"at": now(), "type": error_type, "status": "error", "jobId": identifier}
    if reason:
        error["reason"] = reason
    save_json(state / "last-error.json", error)
    save_json(state / "capture-errors" / (identifier + ".json"), error)


def stop_event(event, config):
    """Enqueue at most once; blocked/uncertain content never becomes injected context."""
    if not event.get("turn_id"):
        return {}
    record_stop_observation(config, event)
    promote_prepared_context(config, event)
    if not config.get("intakeEnabled", True) or event.get("stop_hook_active"):
        return {}
    state = Path(config["stateDir"])
    identifier = event_path(config, event).stem
    if pending_path(config, identifier).exists():
        return {}
    if any((state / folder / (identifier + ".json")).exists() for folder in ("completed", "failed")):
        return {}
    queued = state / "queue" / (identifier + ".json")
    if queued.exists():
        return {}
    record = load_json(event_path(config, event), {})
    record, captured = recover_turn_route(event, record, config)
    if not record.get("projectId"):
        return {}
    if captured is not None:
        save_json(event_path(config, event), record)
        session = session_path(config, event["session_id"])
        previous = load_json(session, {})
        save_json(session, {**previous, "projectId": record["projectId"],
                            "repoIdentity": record["repoIdentity"], "seen": previous.get("seen", {})})
    job = prepare_job(event, record, config, captured)
    pending = list((state / "queue").glob("*.json"))
    if not job.get("intakeFilterReason") and len(pending) >= config.get("maxQueuedJobs", 30):
        if job.get("captureStatus") == "unavailable":
            record_capture_pending(config, identifier, event, record, job,
                                   job.get("captureReason", "named-transcript-unavailable"))
        else:
            record_capture_error(state, identifier, "IntakeQueueFull")
        return {}
    result = enqueue_job(job, queued, config, pending_source=(event, record))
    if queued.exists() or (state / "completed" / queued.name).exists():
        pending_path(config, identifier).unlink(missing_ok=True)
    return result


def prepare_session_job(job, queued, config):
    """Attach prior context before size validation without advancing its cursor."""
    if queued.exists() or not session_enabled(config, job):
        return
    project, session = job.get("projectId"), job.get("sessionId")
    if project and session:
        job["sessionContext"] = context_for_job(config, str(project), str(session))
        if config.get("topicScope") == "semantic" and config.get("exchange", {}).get("protocolVersion") == 2:
            job["topicScope"] = "semantic"
            job["allowedPageIds"] = page_ids(config, project)


def _record_oversize(state, identifier, queued, job, size, maximum):
    """Persist a size rejection while retaining the complete source payload."""
    error = {"at": now(), "type": "JobTooLarge", "status": "error", "jobId": identifier,
             "jobBytes": size, "maxJobBytes": maximum}
    save_json(state / "last-error.json", error)
    save_json(state / "capture-errors" / (identifier + ".json"), error)
    save_json(state / "failed" / queued.name, {**job, **error})


def _queue_session_job(job, queued, config):
    """Persist a source and its session marker using the recoverable queue protocol."""
    if not session_enabled(config, job):
        save_json(queued, job)
        return {}
    return persist_queued_job(config, job, queued)


def _set_legacy_delay(job, config):
    """Keep the old notBefore marker for compatibility with pre-session tooling."""
    policy = config.get("eventDriven", {})
    if policy.get("enabled"):
        delay = min(300, max(0, int(policy.get("debounceSeconds", 120))))
        job["notBefore"] = (datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=delay)).isoformat()


def enqueue_job(job, queued, config, pending_source=None):
    """Persist substantive evidence without running a model in event-driven hooks."""
    state = Path(config["stateDir"])
    identifier = queued.stem
    reason = job.get("intakeFilterReason") or (job_automation_reason(job) if job.get("captureStatus") != "unavailable" else "")
    if reason:
        clear_capture_error(state, identifier)
        save_json(state / "completed" / (identifier + ".json"), {"status": "empty", "reason": reason})
        return {}
    max_bytes = max(1000, int(config.get("maxJobBytes", MAX_JOB_BYTES)))
    prepare_session_job(job, queued, config)
    size = job_bytes(job)
    if size > max_bytes:
        _record_oversize(state, identifier, queued, job, size, max_bytes)
        return {}
    if job.get("captureStatus") == "unavailable":
        if pending_source is not None:
            event, record = pending_source
            record_capture_pending(config, identifier, event, record, job,
                                   job.get("captureReason", "named-transcript-unavailable"))
        else:
            record_capture_error(state, identifier, "EvidenceCaptureUnavailable", job.get("captureReason", ""))
        return {}
    durable = re.search(r"决定|采用|统一|以后|默认|必须|不要|确认|约束|口径|上限|阈值|原则|规范|always|must|decision", job["prompt"], re.I)
    has_artifact = any(item["kind"] == "artifact" for item in job["evidence"])
    if not has_artifact and not (job["evidence"] and durable) and not has_substantive_evidence(job["evidence"]):
        clear_capture_error(state, identifier)
        save_json(state / "completed" / (identifier + ".json"), {"status": "empty", "reason": "no-durable-evidence"})
        return {}
    if not queued.exists():
        _set_legacy_delay(job, config)
        _queue_session_job(job, queued, config)
    clear_capture_error(state, identifier)
    if not config.get("exchange") and not config.get("eventDriven", {}).get("enabled"):
        process_queue(config, limit=1)
    return {}


def handle(event, config):
    """Ignore malformed or excluded events before invoking any model or reading evidence."""
    if not config.get("enabled") or not all(event.get(k) for k in ("cwd", "session_id")):
        return {}
    if any(part.startswith("llmwiki-codex-agent-") for part in Path(event["cwd"]).resolve().parts):
        return {}
    if any(inside(event["cwd"], p) for p in config.get("excludedPaths", [])):
        return {}
    if event.get("hook_event_name") == "SessionStart":
        return session_start_event(event, config)
    if not event.get("turn_id"):
        return {}
    if event.get("hook_event_name") == "UserPromptSubmit":
        return prompt_event(event, config)
    if event.get("hook_event_name") == "Stop":
        return stop_event(event, config)
    return {}


def main():
    """Protocol adapter: valid JSON on stdout even when dependencies are unavailable."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    config = config_from(args.config)
    try:
        result = process_queue(config) if args.drain else handle(json.load(sys.stdin), config)
    except Exception as error:
        save_json(Path(config["stateDir"]) / "last-error.json",
                  {"at": now(), "type": type(error).__name__})
        result = {}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
