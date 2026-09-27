"""Prepare and manage the event-driven knowledge worker on macOS.

This module deliberately keeps launchd concerns separate from the normal
configuration installer.  Rendering and preflight are cross-platform and do
not contact launchd; lifecycle operations are explicit and refuse to claim an
active worker on other platforms.  The event worker only watches private
queue paths and the exchange paths needed by the configured role.
"""

from __future__ import annotations

import os
import plistlib
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EVENT_LABEL = "com.llmwiki.knowledge-flow.wake"
EVENT_PLIST_NAME = EVENT_LABEL + ".plist"
EVENT_MINUTES = tuple(range(0, 60, 5))


@dataclass(frozen=True)
class EventPaths:
    """Private files owned by this event worker installation."""

    config: Path
    launcher: Path
    plist: Path


def event_paths(home: Path, config_path: Path) -> EventPaths:
    """Return stable paths without creating or deleting anything."""
    return EventPaths(config_path.resolve(), (home / ".local/bin/llmwiki-wake").resolve(),
                      (home / "Library/LaunchAgents" / EVENT_PLIST_NAME).resolve())


def _absolute(value: Any, field: str) -> Path:
    """Validate a path from a private expanded configuration."""
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"event worker requires an absolute {field}")
    return Path(value).resolve()


def event_enabled(config: dict[str, Any]) -> bool:
    """Return whether the event worker is explicitly enabled for this role."""
    event = config.get("eventDriven", {})
    exchange = config.get("exchange")
    v2 = isinstance(exchange, dict) and exchange.get("protocolVersion", 1) == 2
    return bool(config.get("enabled", False) and (config.get("intakeEnabled", False) or v2)
                and isinstance(event, dict) and event.get("enabled", False))


def watch_paths(config: dict[str, Any]) -> list[Path]:
    """Build the narrow WatchPaths list for publisher or contributor roles.

    A publisher consumes every participant's immutable submissions.  A
    contributor only needs receipts, which avoids watching the exchange root,
    its own machine directory, or publisher reports/machine status.
    """
    state = _absolute(config.get("stateDir"), "stateDir")
    result = [state / "queue", state / "capture-pending"]
    exchange = config.get("exchange")
    if not isinstance(exchange, dict):
        return result
    root = _absolute(exchange.get("root"), "exchange.root")
    if exchange.get("protocolVersion", 1) == 2:
        participants = exchange.get("participants", [])
        if not isinstance(participants, list) or not all(isinstance(item, str) for item in participants):
            raise ValueError("exchange.participants must be a list")
        publications = root / "v2" / "publications"
        result.append(publications)
        result.extend(publications / item for item in participants)
        result.append(root / "v2" / "baseline.json")
        if exchange.get('sharedWriter') is not None:
            coordination = root / 'v2' / 'shared-writer'
            result.extend([coordination, coordination / 'requests', coordination / 'releases'])
            for directory in ('requests', 'releases'):
                result.extend(coordination / directory / item for item in participants)
        return _unique_paths(result)
    machine = config.get("machineId")
    is_publisher = bool(config.get("publishEnabled")) and machine == exchange.get("publisherMachineId")
    if is_publisher:
        participants = exchange.get("participants", [])
        if not isinstance(participants, list):
            raise ValueError("exchange.participants must be a list")
        result.extend(root / "submissions" / str(item) for item in participants)
    else:
        # Receipts are the only shared signal contributors need after submit.
        result.append(root / "receipts")
    return _unique_paths(result)


def _unique_paths(paths: list[Path]) -> list[Path]:
    """Preserve order while removing duplicate watch paths."""
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def directories_to_create(config: dict[str, Any]) -> list[Path]:
    """Return private queue and role-scoped exchange directories to pre-create."""
    state = _absolute(config.get("stateDir"), "stateDir")
    directories = [state / "queue", state / "capture-pending", state / "reports"]
    exchange = config.get("exchange")
    if isinstance(exchange, dict):
        root = _absolute(exchange.get("root"), "exchange.root")
        if exchange.get("protocolVersion", 1) == 2:
            participants = exchange.get("participants", [])
            if not isinstance(participants, list) or not all(isinstance(item, str) for item in participants):
                raise ValueError("exchange.participants must be a list")
            publications = root / "v2" / "publications"
            directories.extend([root / "v2", publications])
            directories.extend(publications / item for item in participants)
            if exchange.get('sharedWriter') is not None:
                coordination = root / 'v2' / 'shared-writer'
                directories.append(coordination)
                for directory in ('requests', 'releases'):
                    directories.extend(coordination / directory / item for item in participants)
            return _unique_paths(directories)
        machine = config.get("machineId")
        publisher = bool(config.get("publishEnabled")) and machine == exchange.get("publisherMachineId")
        if publisher:
            participants = exchange.get("participants", [])
            if not isinstance(participants, list):
                raise ValueError("exchange.participants must be a list")
            directories.extend(root / "submissions" / str(item) for item in participants)
        else:
            if isinstance(machine, str) and machine:
                directories.append(root / "submissions" / machine)
            directories.append(root / "receipts")
    return _unique_paths(directories)


def render_launcher(config: dict[str, Any], python: str, runtime: Path) -> str:
    """Render wake launcher with an explicit non-interactive executable PATH."""
    wake = runtime / "knowledge-flow/wake.py"
    return "\n".join(("#!/bin/sh", "set -eu", f"export PATH={sh_quote(_controlled_path(config))}",
                        f"exec {sh_quote(python)} {sh_quote(str(wake))} --config {sh_quote(str(Path(config['_configPath']).resolve()))} \"$@\"", ""))


def _controlled_path(config: dict[str, Any]) -> str:
    """Build a minimal launchd PATH, requiring codex only for model intake."""
    directories: list[str] = []
    if config.get("intakeEnabled"):
        codex = shutil.which("codex")
        if not codex:
            raise ValueError("event worker requires executable codex for intake")
        directories.append(str(Path(codex).absolute().parent))
    node = config.get("node")
    if isinstance(node, str) and Path(node).is_absolute():
        directories.append(str(Path(node).parent))
    directories.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    return os.pathsep.join(dict.fromkeys(directories))


def sh_quote(value: str) -> str:
    """Quote one launcher argument using POSIX shell syntax."""
    import shlex
    return shlex.quote(value)


def render_plist(config: dict[str, Any], launcher: Path) -> bytes:
    """Render a launchd plist without unsupported hot-restart settings."""
    paths = [str(path) for path in watch_paths(config)]
    state = _absolute(config["stateDir"], "stateDir")
    payload: dict[str, Any] = {
        "Label": EVENT_LABEL,
        "ProgramArguments": [str(launcher)],
        "WatchPaths": paths,
        "RunAtLoad": True,
        "StartCalendarInterval": [{"Minute": minute} for minute in EVENT_MINUTES],
        "ProcessType": "Background",
        "ThrottleInterval": 30,
        "StandardOutPath": str(state / "reports" / "wake.log"),
        "StandardErrorPath": str(state / "reports" / "wake.err"),
    }
    if "QueueDirectories" in payload or "KeepAlive" in payload:
        raise AssertionError("event worker must not use queue or hot-restart launchd keys")
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def event_plan(config: dict[str, Any], home: Path, runtime: Path, python: str, config_path: Path) -> tuple[EventPaths, str, bytes, list[Path]]:
    """Build the launcher, plist, and directory plan before touching disk."""
    if not event_enabled(config):
        raise ValueError("event worker is not enabled for this configuration")
    paths = event_paths(home, config_path)
    event_config = dict(config)
    event_config["_configPath"] = str(config_path)
    launcher = render_launcher(event_config, python, runtime)
    plist = render_plist(config, paths.launcher)
    directories = directories_to_create(config)
    return paths, launcher, plist, directories


def _launch_command(action: str, launchctl: str, domain: str, plist: Path, label: str) -> list[str]:
    """Build one launchctl command while keeping the lifecycle policy explicit."""
    if action == "enable":
        return [launchctl, "bootstrap", domain, str(plist)]
    if action in {"bootout", "disable"}:
        return [launchctl, "bootout", f"{domain}/{label}"]
    if action == "status":
        return [launchctl, "print", f"{domain}/{label}"]
    raise ValueError(f"unsupported event worker action: {action}")


def launchctl_action(action: str, plist: Path, *, label: str = EVENT_LABEL,
                     dry_run: bool = False, executable: str | None = None,
                     platform_name: str | None = None, uid: int | None = None) -> dict[str, Any]:
    """Run one explicit lifecycle action and verify the resulting service state."""
    system = platform_name or platform.system()
    if str(system).lower() != "darwin":
        return {"action": action, "supported": False, "active": False,
                "success": False, "confirmedInactive": False,
                "reason": "event worker activation is macOS-only"}
    launchctl = executable or shutil.which("launchctl")
    if not launchctl:
        raise ValueError("launchctl was not found")
    target_uid = os.getuid() if uid is None else uid
    domain = f"gui/{target_uid}"
    command = _launch_command(action, launchctl, domain, plist, label)
    if dry_run:
        return {"action": action, "supported": True, "dryRun": True, "command": command}
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    payload = {"action": action, "supported": True, "active": result.returncode == 0,
               "returncode": result.returncode, "stdout": result.stdout[-4000:],
               "stderr": result.stderr[-4000:], "command": command}
    if action == "status":
        payload["confirmed"] = True
        return payload
    verification = _service_status(launchctl, domain, label)
    payload["verification"] = verification
    payload["active"] = verification["active"]
    if action in {"bootout", "disable"}:
        payload["confirmedInactive"] = bool(verification["absent"])
        payload["success"] = payload["confirmedInactive"]
    else:
        output = (result.stdout + "\n" + result.stderr).lower()
        already_loaded = (result.returncode != 0 and verification["active"]
                          and "already" in output
                          and any(word in output for word in ("loaded", "bootstrapped", "exists")))
        payload["alreadyLoaded"] = already_loaded
        payload["success"] = bool(verification["active"] and (result.returncode == 0 or already_loaded))
    return payload


def _service_status(launchctl: str, domain: str, label: str) -> dict[str, Any]:
    """Read launchd state after a mutating action; never infer it from bootstrap."""
    command = [launchctl, "print", f"{domain}/{label}"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout + "\n" + result.stderr).lower()
    absent = result.returncode != 0 and (result.returncode in {3, 113}
             or "could not find service" in output or "could not find specified service" in output
             or "no such process" in output)
    return {"active": result.returncode == 0, "absent": absent, "returncode": result.returncode,
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
            "command": command}


def remove_owned_files(paths: EventPaths, *, dry_run: bool = False) -> list[str]:
    """Remove only this install's launcher and plist; queue state is untouched."""
    removed: list[str] = []
    for path in (paths.launcher, paths.plist):
        if path.exists():
            removed.append(str(path))
            if not dry_run:
                path.unlink()
    return removed


__all__ = ["EVENT_LABEL", "EVENT_PLIST_NAME", "EventPaths", "directories_to_create",
           "event_enabled", "event_paths", "event_plan", "launchctl_action", "remove_owned_files",
           "render_plist", "watch_paths"]
