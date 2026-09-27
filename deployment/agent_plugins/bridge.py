"""Dispatch native host events into the configured shared knowledge workflow.

The launcher starts a fresh process per event. This module reads the one
knowledge-flow config each time, imports hooks from its current worker path,
and leaves routing, intake and idempotency to that shared runtime.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_CONFIG = Path.home() / ".config/llmwiki/knowledge-flow.json"
MAX_EVIDENCE_CHARS = 120_000


def load_shared(config_path: Path) -> tuple[dict[str, Any], Any, Any]:
    """Load the configured runtime by its live worker pointer."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    worker = Path(raw.get("worker", ""))
    if not worker.is_absolute() or not (worker.parent / "hooks.py").is_file():
        raise ValueError("configured worker is unavailable")
    sys.path.insert(0, str(worker.parent))
    common = importlib.import_module("common")
    hooks = importlib.import_module("hooks")
    return common.config_from(config_path), common, hooks


def registration(config: dict[str, Any], host: str, config_path: Path, profile: Path) -> dict[str, Any] | None:
    """Require an exact active registration before any host event is handled."""
    plugins = config.get("agentPlugins")
    if not isinstance(plugins, dict) or plugins.get("version") != 1:
        return None
    records = plugins.get("registrations")
    if not isinstance(records, list):
        return None
    matches = [item for item in records if isinstance(item, dict) and item.get("host") == host
               and item.get("config") == str(config_path.resolve())
               and item.get("profile") == str(profile.resolve())]
    if len(matches) > 1:
        raise ValueError("duplicate host registration")
    return matches[0] if matches else None


def pi_event(payload: dict[str, Any], action: str, common: Any,
             config_path: Path, profile: Path) -> dict[str, Any] | None:
    """Bind a Pi callback and its verified native evidence to the core event."""
    session = payload.get("sessionId")
    turn = payload.get("turnId")
    source_profile = payload.get("profile")
    cwd = payload.get("cwd")
    prompt = payload.get("prompt")
    if (not all(isinstance(value, str) and value for value in (session, turn, cwd, prompt, source_profile))
            or Path(source_profile).resolve() != profile.resolve()):
        return None
    if not Path(cwd).is_absolute():
        return None
    event = {"hook_event_name": "UserPromptSubmit" if action == "prompt" else "Stop",
             "session_id": "pi:" + common.digest(str(profile.resolve()))[:12] + ":" + common.digest(session)[:16],
             "turn_id": "pi:" + turn,
             "cwd": cwd, "prompt": prompt}
    if action == "prompt":
        return event
    evidence = validate_pi_evidence(payload.get("evidence"), payload, common)
    if payload.get("outcome") != "completed" or evidence is None:
        return None
    event.update(last_assistant_message=next(item["text"] for item in reversed(evidence)
                                             if item["kind"] == "assistant"),
                 conversation_evidence=evidence)
    return event


def validate_pi_evidence(value: Any, payload: dict[str, Any], common: Any) -> list[dict[str, Any]] | None:
    """Accept only one bounded, complete current-turn branch with native locators."""
    raw = raw_pi_evidence(value, payload, common)
    return sanitize_pi_evidence(raw, common) if raw is not None else None


def raw_pi_evidence(value: Any, payload: dict[str, Any], common: Any) -> list[dict[str, Any]] | None:
    """Validate every raw item, locator, hash and aggregate limit before cleanup."""
    if not isinstance(value, list) or not value or len(value) > 200:
        return None
    session, prompt = payload["sessionId"], payload["prompt"]
    prompt_entry = payload.get("promptEntryId")
    if not isinstance(prompt_entry, str) or not prompt_entry:
        return None
    expected_prefix = f"pi://{quote(session, safe='')}/entry/"
    result, total, ids = [], 0, set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or item.get("kind") not in ("user", "assistant"):
            return None
        identifier, text, locator = item.get("id"), item.get("text"), item.get("locator")
        native_locator = expected_prefix + quote(identifier, safe="") if isinstance(identifier, str) else ""
        if (not isinstance(identifier, str) or not identifier or identifier in ids
                or not isinstance(text, str) or not text.strip()
                or item.get("complete") is not True or item.get("current") is not True
                or not isinstance(locator, str) or locator != native_locator
                or item.get("sha256") != common.digest(text)):
            return None
        if index == 0 and (item["kind"] != "user" or identifier != prompt_entry or text.strip() != prompt.strip()):
            return None
        if index > 0 and item["kind"] != "assistant":
            return None
        ids.add(identifier)
        if len(text) > MAX_EVIDENCE_CHARS:
            return None
        total += len(text)
        if total > MAX_EVIDENCE_CHARS:
            return None
        result.append({"id": identifier, "kind": item["kind"], "text": text,
                       "locator": locator, "observedAt": str(item.get("observedAt", ""))[:80]})
    if not any(item["kind"] == "assistant" for item in result):
        return None
    return result


def sanitize_pi_evidence(raw: list[dict[str, Any]], common: Any) -> list[dict[str, Any]]:
    """Redact only after the complete native payload passes its size checks."""
    return [{"id": "pi-" + common.digest(item["id"])[:24], "kind": item["kind"],
             "text": common.safe_text(item["text"], MAX_EVIDENCE_CHARS),
             "locator": item["locator"], "observedAt": item["observedAt"], "complete": True}
            for item in raw]


def dispatch(host: str, payload: dict[str, Any], config_path: Path, profile: Path | None = None) -> dict[str, Any]:
    """Adapt one host event and delegate it to the shared hooks implementation."""
    config, common, hooks = load_shared(config_path)
    if profile is None or not registration(config, host, config_path, profile):
        return {}
    if host == "claude":
        from claude_adapter import handle

        return handle(payload, config, profile, common, hooks)
    action = payload.get("action")
    if host != "pi" or action not in ("prompt", "stop"):
        raise ValueError("unsupported host event")
    event = pi_event(payload, action, common, config_path, profile)
    if event is None:
        return {}
    return hooks.handle(event, config)


def parser() -> argparse.ArgumentParser:
    """Build the private bridge command used by the stable host launcher."""
    result = argparse.ArgumentParser()
    result.add_argument("--host", choices=("claude", "pi"), required=True)
    result.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    result.add_argument("--profile", type=Path)
    result.add_argument("--input-file", type=Path,
                        help="private temporary JSON event file used by Pi's subprocess API")
    return result


def main() -> None:
    """Emit only the host response; diagnostics never include event content."""
    args = parser().parse_args()
    try:
        if args.input_file:
            input_path = args.input_file.expanduser().resolve(strict=True)
            if not input_path.is_file() or input_path.stat().st_mode & 0o077 or input_path.stat().st_size > 1_000_000:
                raise ValueError("Pi event file is not private or exceeds the limit")
            payload = json.loads(input_path.read_text(encoding="utf-8"))
        else:
            payload = json.load(sys.stdin)
        result = dispatch(args.host, payload, args.config.expanduser().resolve(),
                          args.profile.expanduser().resolve() if args.profile else None)
    except Exception as error:
        print(f"llmwiki agent bridge unavailable ({type(error).__name__})", file=sys.stderr)
        result = {}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
