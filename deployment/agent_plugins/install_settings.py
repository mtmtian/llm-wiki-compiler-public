"""Exact, lossless edits to the shared registry and native host settings."""

from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path
from typing import Any

from .install_storage import PlannedWrites, Snapshot, remember_read

CLAUDE_EVENTS = ("UserPromptSubmit", "Stop")
EVENT_TIMEOUTS = {"UserPromptSubmit": 8, "Stop": 15}


def read_json(path: Path, default: Any = None,
              read_set: dict[Path, Snapshot] | None = None) -> Any:
    """Read optional JSON while distinguishing absence from corruption."""
    if read_set is not None:
        remember_read(read_set, path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON: {path}") from error


def registration_record(records: list[Any], host: str, config: Path,
                        profile: Path) -> dict[str, Any] | None:
    """Find one exact host/config/profile registration and reject ambiguity."""
    matches = [item for item in records if isinstance(item, dict) and item.get("host") == host
               and item.get("config") == str(config) and item.get("profile") == str(profile)]
    if len(matches) > 1:
        raise ValueError("duplicate agentPlugins registration")
    return matches[0] if matches else None


def registry_data(config: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    """Validate versioned ownership metadata while retaining future fields."""
    existing = config.get("agentPlugins")
    if existing is None:
        return {"version": 1}, []
    if not isinstance(existing, dict) or existing.get("version") != 1:
        raise ValueError("agentPlugins registry version is unsupported")
    records = existing.get("registrations", [])
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError("agentPlugins registrations must be objects")
    return copy.deepcopy(existing), list(records)


def current_command(launcher: Path, config: Path, profile: Path) -> str:
    """Render the exact stable-launcher command installed in a Claude hook."""
    return shlex.join([str(launcher), "--host", "claude", "--config", str(config),
                       "--profile", str(profile)])


def legacy_command(entry: Any, config: Path, profile: Path, integration_root: Path) -> bool:
    """Recognize a previous bridge only when config and profile both match."""
    if not isinstance(entry, dict) or entry.get("type") != "command" or not isinstance(entry.get("command"), str):
        return False
    try:
        tokens = shlex.split(entry["command"])
    except ValueError:
        return False
    if not tokens or not Path(tokens[0]).name.startswith("python"):
        return False
    script_index = 2 if len(tokens) == 7 and tokens[1] == "-B" else 1
    if len(tokens) != script_index + 5:
        return False
    script = Path(tokens[script_index]).expanduser().resolve()
    digest_dir = script.parent.name
    return (script.name == "claude_hook.py" and script.parent.parent.resolve() == integration_root.resolve()
            and len(digest_dir) == 12 and all(char in "0123456789abcdef" for char in digest_dir)
            and tokens[script_index + 1] == "--config"
            and Path(tokens[script_index + 2]).expanduser().resolve() == config
            and tokens[script_index + 3] == "--claude-config-dir"
            and Path(tokens[script_index + 4]).expanduser().resolve() == profile)


def owned_claude_entry(entry: Any, command: str, config: Path, profile: Path,
                       integration_root: Path) -> bool:
    """Identify only this exact current or legacy hook command."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    return entry.get("command") == command or legacy_command(entry, config, profile, integration_root)


def normalize_hook_groups(groups: list[Any], command: str, config: Path, profile: Path,
                          integration_root: Path, desired: dict[str, Any], installing: bool
                          ) -> tuple[list[Any], bool]:
    """Remove exact owned entries, retaining every unrelated group and hook."""
    kept_groups, changed = [], False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            raise ValueError("Claude hook group is malformed")
        old = group["hooks"]
        kept = [entry for entry in old if not owned_claude_entry(entry, command, config, profile,
                                                                  integration_root)]
        changed |= len(kept) != len(old)
        if kept or not old:
            updated = copy.deepcopy(group)
            updated["hooks"] = kept
            kept_groups.append(updated)
    if installing:
        kept_groups.append({"hooks": [desired]})
        changed = True
    return kept_groups, changed


def claude_settings(path: Path, profile: Path, config: Path, launcher: Path,
                    integration_root: Path, installing: bool,
                    read_set: dict[Path, Snapshot] | None = None) -> str | None:
    """Add/remove two hooks for one config/profile and preserve other settings."""
    original = read_json(path, {"hooks": {}}, read_set)
    if not isinstance(original, dict):
        raise ValueError("Claude settings must be an object")
    result = copy.deepcopy(original)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude hooks must be an object")
    changed = False
    command = current_command(launcher, config, profile)
    for event in CLAUDE_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Claude {event} hooks must be an array")
        desired = {"type": "command", "command": command, "timeout": EVENT_TIMEOUTS[event]}
        if installing and exact_hook_present(groups, desired, command, config, profile, integration_root):
            continue
        normalized, altered = normalize_hook_groups(groups, command, config, profile,
                                                     integration_root, desired, installing)
        changed |= altered
        if normalized:
            hooks[event] = normalized
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n" if changed else None


def exact_hook_present(groups: list[Any], desired: dict[str, Any], command: str,
                       config: Path, profile: Path, integration_root: Path) -> bool:
    """Require exactly one correct entry across both current and legacy hooks."""
    if any(not isinstance(group, dict) or not isinstance(group.get("hooks"), list) for group in groups):
        return False
    matches = [group for group in groups if isinstance(group, dict) and group.get("hooks") == [desired]]
    owned = [entry for group in groups for entry in group["hooks"]
             if owned_claude_entry(entry, command, config, profile, integration_root)]
    return len(matches) == 1 and len(owned) == 1


def pi_settings(path: Path, extension: Path, installing: bool, previously_owned: bool,
                read_set: dict[Path, Snapshot] | None = None) -> str | None:
    """Merge one stable extension path and never adopt an unowned registration."""
    original = read_json(path, {}, read_set)
    if not isinstance(original, dict):
        raise ValueError("Pi settings must be an object")
    result = copy.deepcopy(original)
    values = result.get("extensions", [])
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError("Pi extensions must be a string array")
    target = str(extension)
    if installing and target in values and not previously_owned:
        raise ValueError("Pi extension path is already registered outside this installer")
    if not installing and not previously_owned:
        return None
    filtered = [item for item in values if item != target]
    if installing:
        filtered.append(target)
    if filtered == values:
        return None
    result["extensions"] = filtered
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def unique_plan(plan: list[tuple[Path, str, int]]) -> list[tuple[Path, str, int]]:
    """Reject conflicting writes before any destination is touched."""
    seen: dict[Path, tuple[str, int]] = {}
    for path, content, mode in plan:
        target, value = Path(path.absolute()), (content, mode)
        if target in seen and seen[target] != value:
            raise ValueError(f"conflicting installer plans for {target}")
        seen[target] = value
    entries = [(path, content, mode) for path, (content, mode) in seen.items()]
    return PlannedWrites(entries, read_set=getattr(plan, "read_set", {}),
                         current=getattr(plan, "current_expected", None))
