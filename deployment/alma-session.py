"""Bridge one Alma thread into the existing llm-wiki Stop intake path.

Alma currently exposes thread reading through its CLI, not a native hook API.
This adapter reads every visible user/assistant message with paginated
``alma thread messages --full --json`` calls, fails closed on incomplete pages,
and submits one synthetic Stop event to the existing routing and queue code.
It does not create a second queue or inject context into an already-sent Alma
turn; Alma retrieval remains an explicit MCP/tool call until Alma exposes a
supported prompt hook.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PAGE_SIZE = 80
MAX_MESSAGES = 400
MAX_EVIDENCE_CHARS = 120_000


def read_page(thread_id: str, offset: int) -> dict:
    """Read and validate one Alma pagination envelope, failing closed on loss."""
    result = subprocess.run(
        ["alma", "thread", "messages", thread_id, str(PAGE_SIZE), "--full", "--offset", str(offset), "--json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    envelope = json.loads(result.stdout)
    required = ("messages", "hasMore", "nextOffset", "missingMessageIds", "sourceTextTruncated")
    if not isinstance(envelope, dict) or any(key not in envelope for key in required):
        raise ValueError("Alma returned an incomplete pagination envelope")
    messages = envelope["messages"]
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("Alma returned an invalid message page")
    if type(envelope["hasMore"]) is not bool or type(envelope["nextOffset"]) is not int:
        raise ValueError("Alma returned invalid pagination cursors")
    if envelope["nextOffset"] < 0 or envelope["nextOffset"] != offset + len(messages):
        raise ValueError("Alma returned a discontinuous pagination cursor")
    if (not isinstance(envelope["missingMessageIds"], list)
            or any(not isinstance(identifier, str) or not identifier for identifier in envelope["missingMessageIds"])):
        raise ValueError("Alma returned invalid missing message metadata")
    if envelope["missingMessageIds"] or envelope["sourceTextTruncated"] is not False:
        raise ValueError("Alma returned incomplete message content")
    return envelope


def read_thread(thread_id: str) -> list[dict]:
    """Read all pages and reject an unbounded or duplicated transcript."""
    messages: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while True:
        envelope = read_page(thread_id, offset)
        page = envelope["messages"]
        if len(messages) + len(page) > MAX_MESSAGES:
            raise ValueError("Alma thread exceeds the evidence limit")
        for message in page:
            identifier = str(message.get("id", ""))
            if not identifier or identifier in seen:
                raise ValueError("Alma returned duplicate or unidentified messages")
            seen.add(identifier)
            messages.append(message)
        if not envelope["hasMore"]:
            return messages
        if not page or envelope["nextOffset"] <= offset:
            raise ValueError("Alma returned a non-advancing pagination cursor")
        offset = envelope["nextOffset"]


def message_role(message: dict) -> str | None:
    """Read a message role without trusting the host payload shape."""
    body = message.get("message", message)
    return body.get("role") if isinstance(body, dict) else None


def visible_text(message: dict) -> str:
    """Extract text parts only; reasoning and tool payloads are excluded."""
    body = message.get("message", message)
    parts = body.get("parts", []) if isinstance(body, dict) else []
    texts = [part.get("text", "") for part in parts
             if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
    return "\n".join(texts).strip()


def evidence(messages: list[dict]) -> list[dict]:
    """Build hashed, complete visible user/assistant evidence for the worker."""
    from common import digest, safe_text
    from hooks import now

    result = []
    total = 0
    for message in messages:
        role = message_role(message)
        if role not in ("user", "assistant"):
            continue
        text = safe_text(visible_text(message), MAX_EVIDENCE_CHARS)
        if not text:
            continue
        total += len(text)
        if total > MAX_EVIDENCE_CHARS:
            raise ValueError("Alma transcript exceeds the evidence budget")
        identifier = str(message["id"])
        result.append({"id": "alma-" + digest(identifier)[:24], "kind": role, "text": text,
                       "locator": "alma://thread/" + identifier,
                       "observedAt": str(message.get("createdAt") or now())})
    return result


def build_event(thread_id: str, cwd: str, messages: list[dict]) -> dict:
    """Create the existing Stop protocol from the latest visible turn."""
    from common import safe_text
    from hooks import now

    visible = [(message, visible_text(message)) for message in messages]
    users = [(message, text) for message, text in visible if message_role(message) == "user" and text]
    assistants = [(message, text) for message, text in visible if message_role(message) == "assistant" and text]
    if not users:
        raise ValueError("Alma thread has no visible user message")
    latest_user, prompt = users[-1]
    return {"hook_event_name": "Stop", "session_id": thread_id, "turn_id": str(latest_user["id"]),
            "cwd": cwd, "prompt": safe_text(prompt, 12000),
            "last_assistant_message": safe_text(assistants[-1][1], 16000) if assistants else "",
            "conversation_evidence": evidence(messages)}


def submit(config: dict, thread_id: str, cwd: str) -> dict:
    """Apply the same routing gate before handing off to the existing Stop path."""
    from common import save_json
    from hooks import event_path, handle, now
    from routing import resolve

    messages = read_thread(thread_id)
    event = build_event(thread_id, cwd, messages)
    project, reason = resolve(cwd, event["prompt"], None, config)
    record = {"projectId": project, "reason": reason, "createdAt": now()}
    if project:
        record["prompt"] = event["prompt"]
    save_json(event_path(config, event), record)
    if not project:
        return {"status": "filtered", "reason": reason}
    return {"status": "submitted", "projectId": project, "result": handle(event, config)}


def main() -> None:
    """Read one Alma thread and print a machine-readable intake result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--thread", default=os.environ.get("ALMA_THREAD_ID"))
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args()
    if not args.thread:
        parser.error("--thread or ALMA_THREAD_ID is required")
    raw_config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(raw_config["worker"]).parent))
    from common import config_from

    config = config_from(args.config)
    try:
        result = submit(config, args.thread, args.cwd)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
        result = {"status": "error", "error": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()


__all__ = ["build_event", "evidence", "read_thread", "submit"]
