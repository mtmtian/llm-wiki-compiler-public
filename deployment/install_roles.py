"""Validate shared-machine roles and immutable runtime release metadata.

Protocol v1 retains one designated publisher for migration.  Protocol v2 lets
each declared participant publish its own immutable record and rebuild indexes
locally, so role switches remain explicit at every machine.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


MACHINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RoleRequest:
    """Normalized role switches accepted by the installer CLI."""

    writer: bool = False
    contributor: bool = False
    publisher: bool = False
    reader: bool = False

    @classmethod
    def from_args(cls, args: Any) -> "RoleRequest":
        """Read role switches from argparse or a test Namespace."""
        return cls(*(bool(getattr(args, name, False)) for name in
                     ("writer", "contributor", "publisher", "reader")))


def validate_machine_id(value: Any) -> str:
    """Require a portable stable identity suitable for exchange paths."""
    if not isinstance(value, str) or not MACHINE_ID_PATTERN.fullmatch(value):
        raise ValueError("machineId must be a stable identifier (letters, digits, . _ -)")
    return value


def validate_exchange(value: Any) -> dict[str, Any]:
    """Validate v1 or independent-publisher v2 exchange settings."""
    if not isinstance(value, dict):
        raise ValueError("exchange must be an object")
    protocol = value.get("protocolVersion", 1)
    if isinstance(protocol, bool) or not isinstance(protocol, int) or protocol not in (1, 2):
        raise ValueError("exchange.protocolVersion must be 1 or 2")
    root = value.get("root")
    participants = value.get("participants")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("exchange.root must be an absolute path")
    if not isinstance(participants, list) or not participants or any(
            not isinstance(item, str) for item in participants):
        raise ValueError("exchange.participants must be a non-empty string list")
    normalized = [validate_machine_id(item) for item in participants]
    if len(set(normalized)) != len(normalized):
        raise ValueError("exchange.participants must be unique")
    if "materializerMachineId" in value and (protocol != 2 or value["materializerMachineId"] not in normalized):
        raise ValueError("exchange.materializerMachineId requires a v2 participant")
    if value.get('sharedWriter') is not None:
        writer = value['sharedWriter']
        if (not isinstance(writer, dict) or set(writer) != {'version', 'bootstrapMachineId'}
                or type(writer['version']) is not int or writer['version'] != 1
                or protocol != 2 or writer['bootstrapMachineId'] not in normalized
                or value.get('materializerMachineId') not in normalized):
            raise ValueError('invalid exchange.sharedWriter configuration')
    if protocol == 1:
        publisher = validate_machine_id(value.get("publisherMachineId"))
        if publisher not in normalized:
            raise ValueError("exchange publisherMachineId must be a participant")
    elif "publisherMachineId" in value:
        raise ValueError("v2 exchange must not define publisherMachineId")
    if "legacyImporterMachineId" in value:
        importer = validate_machine_id(value.get("legacyImporterMachineId"))
        if importer not in normalized:
            raise ValueError("exchange legacyImporterMachineId must be a participant")
    return value


def is_v2_exchange(exchange: dict[str, Any] | None) -> bool:
    """Return whether an exchange uses independent publication records."""
    return bool(exchange and exchange.get("protocolVersion", 1) == 2)


def select_machine_id(machine: dict[str, Any], explicit: Any) -> str | None:
    """Prefer an explicit CLI identity over machine.json without inventing one."""
    candidate = explicit if explicit is not None else machine.get("machineId")
    return None if candidate is None else validate_machine_id(candidate)


def legacy_intake_enabled(existing: dict[str, Any] | None, hooks: dict[str, Any],
                          config_path: Path, hook_matcher: Callable[[Any, Path], bool]) -> bool:
    """Infer the former writer state only when no explicit role was requested."""
    if not existing:
        return False
    if "intakeEnabled" in existing:
        return bool(existing["intakeEnabled"])
    stop_groups = hooks.get("hooks", {}).get("Stop", [])
    installed = any(
        isinstance(group, dict) and any(hook_matcher(entry, config_path)
                                        for entry in group.get("hooks", []))
        for group in stop_groups
    )
    return bool(existing.get("enabled")) and installed


def resolve_roles(existing: dict[str, Any] | None, hooks: dict[str, Any], config_path: Path,
                  request: RoleRequest, exchange: dict[str, Any] | None,
                  machine_id: str | None, hook_matcher: Callable[[Any, Path], bool]) -> tuple[bool, bool]:
    """Resolve explicit intake and publication for legacy and v2 exchanges."""
    selected = sum((request.writer, request.contributor, request.publisher, request.reader))
    if selected > 1:
        raise ValueError("role options are mutually exclusive")
    if request.publisher:
        if exchange is None:
            raise ValueError("--publisher requires exchange")
        if not is_v2_exchange(exchange) and machine_id != exchange["publisherMachineId"]:
            raise ValueError("--publisher requires the designated publisher machine")
        return True, True
    if request.contributor:
        if exchange is None:
            raise ValueError("--contributor requires exchange")
        return True, False
    if request.writer:
        return True, is_v2_exchange(exchange)
    if request.reader:
        return False, False
    return legacy_intake_enabled(existing, hooks, config_path, hook_matcher), False


def configure_roles(config: dict[str, Any], machine: dict[str, Any], args: Any,
                    existing: dict[str, Any] | None, hooks: dict[str, Any], config_path: Path,
                    hook_matcher: Callable[[Any, Path], bool]) -> tuple[bool, bool]:
    """Apply identity and exchange role policy to one expanded flow config."""
    machine_id = select_machine_id(machine, getattr(args, "machine_id", None))
    exchange = validate_exchange(config["exchange"]) if config.get("exchange") is not None else None
    if exchange is not None:
        if machine_id is None:
            raise ValueError("exchange requires machineId from machine.json or --machine-id")
        if machine_id not in exchange["participants"]:
            raise ValueError("machineId must be listed in exchange.participants")
        config["machineId"] = machine_id
    elif machine_id is not None:
        config["machineId"] = machine_id
    request = RoleRequest.from_args(args)
    intake, publish = resolve_roles(existing, hooks, config_path, request,
                                    exchange, machine_id, hook_matcher)
    if is_v2_exchange(exchange) and intake and not publish and "legacyImporterMachineId" not in exchange:
        raise ValueError("v2 contributor requires legacyImporterMachineId")
    if exchange is not None:
        config["publishEnabled"] = publish
    return intake, publish


def validate_runtime_manifest(runtime: Path, required_files: tuple[str, ...]) -> None:
    """Require a complete, hash-verified build manifest for the runtime."""
    manifest_path = runtime / "build-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("runtime build-manifest.json is missing or invalid") from error
    if not isinstance(manifest, dict) or not COMMIT_PATTERN.fullmatch(str(manifest.get("commit", ""))):
        raise ValueError("runtime build manifest has an invalid commit")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("runtime build manifest has no file hashes")
    for relative in required_files:
        if relative not in files:
            raise ValueError(f"runtime manifest is missing {relative}")
    for name, expected in files.items():
        if not isinstance(name, str):
            raise ValueError("runtime manifest contains an unsafe file path")
        target = Path(name)
        if target.is_absolute() or ".." in target.parts:
            raise ValueError("runtime manifest contains an unsafe file path")
        if not isinstance(expected, str) or not SHA256_PATTERN.fullmatch(expected):
            raise ValueError(f"runtime manifest has an invalid hash for {name}")
        path = runtime / target
        if not path.is_file():
            raise ValueError(f"runtime manifest file is missing: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"runtime manifest hash mismatch: {name}")
