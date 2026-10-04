"""Adapt Claude Code's native hooks while keeping policy in shared hooks.py.

Only the selected profile's main-session prompt branch is eligible. Transcript
capture keeps visible user/assistant text and native message locators; tools,
thinking, sidechains, other turns, and files outside that profile are excluded.
An unrouted turn whose transcript fails validation ends filtered, matching the
shared Stop path; only routed turns record a capture error, with a reason code.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from claude_capture import IncompleteTranscript, evidence

# Capture failures raise fixed kebab-case codes; anything else is reduced to its type name.
REASON_CODE = re.compile(r"[a-z]+(?:-[a-z]+)*")
MAX_FLUSH_WAIT_SECONDS = 2.0
FLUSH_POLL_INTERVAL_SECONDS = 0.05


def project_roots(config: dict[str, Any]) -> frozenset[Path]:
    """Configured project directories bound a turn's directory changes like Git worktrees."""
    return frozenset(Path(path).resolve() for project in config.get("projects", {}).values()
                     for path in project.get("paths", [])
                     if isinstance(path, str) and Path(path).is_absolute())


def transcript_size(event: dict[str, Any]) -> int | None:
    """Return the current transcript size, or None while the file is absent."""
    try:
        return Path(event["transcript_path"]).stat().st_size
    except FileNotFoundError:
        return None


def capture_after_flush(event: dict[str, Any], profile: Path, roots: frozenset[Path],
                        clock=None, sleeper=None) -> list[dict[str, Any]]:
    """Retry incomplete reads only after file growth, within Claude's hook budget."""
    now = clock or time.monotonic
    wait = sleeper or time.sleep
    deadline = now() + MAX_FLUSH_WAIT_SECONDS
    previous_size = object()
    last_error: Exception = IncompleteTranscript("transcript-unavailable")
    while True:
        size = transcript_size(event)
        if size != previous_size:
            try:
                return evidence(event, profile, roots)
            except (IncompleteTranscript, FileNotFoundError) as error:
                last_error = (IncompleteTranscript("transcript-unavailable")
                              if isinstance(error, FileNotFoundError) else error)
            previous_size = size
        remaining = deadline - now()
        if remaining <= 0:
            raise last_error
        wait(min(FLUSH_POLL_INTERVAL_SECONDS, remaining))


def normalize(event: dict[str, Any], profile: Path, common: Any) -> dict[str, Any]:
    """Namespace host session and turn IDs without changing their source locators."""
    return {**event, "session_id": "claude:" + common.digest(str(profile.resolve()))[:16] + ":" + event["session_id"],
            "turn_id": "claude:" + event["prompt_id"]}


def stop(event: dict[str, Any], adapted: dict[str, Any], config: dict[str, Any],
         profile: Path, common: Any, hooks: Any) -> dict[str, Any]:
    """Submit a complete native branch through the shared idempotent stop path."""
    path = hooks.event_path(config, adapted)
    record = common.load_json(path, {})
    if not record or not config.get("intakeEnabled", True):
        return {}
    state = Path(config["stateDir"])
    if any((state / folder / path.name).exists() for folder in ("queue", "completed", "failed")):
        return {}
    try:
        adapted["conversation_evidence"] = capture_after_flush(event, profile, project_roots(config))
    except (OSError, ValueError, KeyError, TypeError) as error:
        if not record.get("projectId"):
            # Shared intake also filters unrouted turns whose evidence cannot be validated.
            record_status(config, adapted, "filtered", common, hooks)
            return {}
        return capture_error(config, adapted, error, common, hooks)
    try:
        result = hooks.handle(adapted, config)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return capture_error(config, adapted, error, common, hooks)
    status = next((folder for folder in ("queue", "completed", "failed", "capture-errors")
                   if (state / folder / path.name).exists()), "filtered")
    record_status(config, adapted, status, common, hooks)
    return result


def capture_error(config: dict[str, Any], adapted: dict[str, Any], error: Exception,
                  common: Any, hooks: Any) -> dict[str, Any]:
    """Keep a routed turn's loss visible with a content-free reason code."""
    reason = str(error) if REASON_CODE.fullmatch(str(error)) else type(error).__name__
    hooks.record_capture_error(Path(config["stateDir"]), hooks.event_path(config, adapted).stem,
                               "ClaudeEvidenceUnavailable", reason)
    record_status(config, adapted, "capture-error", common, hooks)
    return {}


def record_status(config: dict[str, Any], event: dict[str, Any], status: str,
                  common: Any, hooks: Any, injected: bool | None = None) -> None:
    """Keep diagnostics content-free and scoped to one stable native event."""
    path = Path(config["stateDir"]) / "agent-events" / hooks.event_path(config, event).name
    value = {"at": hooks.now(), "host": "claude", "status": status,
             "sessionId": event["session_id"], "turnId": event["turn_id"]}
    if injected is not None:
        value["contextInjected"] = injected
    common.save_json(path, value)


def handle(event: dict[str, Any], config: dict[str, Any], profile: Path,
           common: Any, hooks: Any) -> dict[str, Any]:
    """Handle only native prompt and Stop events for an explicit profile."""
    required = ("cwd", "session_id", "prompt_id", "transcript_path")
    if (not config.get("enabled") or event.get("agent_id") or event.get("stop_hook_active")
            or any(not isinstance(event.get(key), str) or not event[key] for key in required)):
        return {}
    if not Path(event["cwd"]).is_absolute() or any(common.inside(event["cwd"], path)
                                                  for path in config.get("excludedPaths", [])):
        return {}
    adapted = normalize(event, profile, common)
    if event.get("hook_event_name") == "UserPromptSubmit":
        result = hooks.handle(adapted, config)
        context = result.get("hookSpecificOutput", {}).get("additionalContext")
        record_status(config, adapted, "prompt", common, hooks, bool(context))
        return result
    if event.get("hook_event_name") == "Stop":
        return stop(event, adapted, config, profile, common, hooks)
    return {}
