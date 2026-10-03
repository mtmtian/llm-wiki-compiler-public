"""Activate a shared contract only after every replica participant can read it.

A gate names one reader capability and one shared policy file. Readiness is
attested by each participant's announcement of its installed runtime, never by
a local configuration flag; the local announcement must also match the runtime
actually installed. Activation writes one immutable policy that every host
reads, so all participants switch together and a missing or downgraded peer
fails closed. Semantic topics (``semantic_scope``) and the knowledge ledger
(``ledger_gate``) are two gates with independent capabilities and policies.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replica_records import IDENTITY
from shared_files import SharedFiles

MAX_STATUS_BYTES = 16384


@dataclass(frozen=True)
class Gate:
    """One reader capability and the shared policy that activates it."""

    name: str          # appears in operator messages, e.g. "semantic topics"
    capability: str    # runtime manifest and announcement capability
    policy_path: str   # shared policy file under the exchange root
    field: str         # policy marker key, e.g. "topicScope"
    value: str         # policy marker value, e.g. "semantic"


def runtime_manifest(config: dict[str, Any]) -> dict[str, Any]:
    """Read the installed release manifest, never a claimed configuration flag."""
    path = Path(config.get("worker", "")).parent.parent / "build-manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def participants(config: dict[str, Any], gate: Gate) -> list[str]:
    """Require a concrete v2 replica set before consulting shared attestations."""
    settings = config.get("exchange", {})
    peers = settings.get("participants")
    if settings.get("protocolVersion") != 2 or not isinstance(peers, list) or not peers:
        raise ValueError(f"{gate.name} require v2 replica participants")
    if any(not isinstance(peer, str) or not IDENTITY.fullmatch(peer) for peer in peers):
        raise ValueError(f"invalid {gate.name} replica participant")
    if len(set(peers)) != len(peers) or config.get("machineId") not in peers:
        raise ValueError(f"{gate.name} replica participants must include this machine exactly once")
    return sorted(peers)


def shared_json(config: dict[str, Any], relative: str) -> Any:
    """Read bounded shared JSON without following symlinks or hiding corruption."""
    root = Path(config["exchange"]["root"])
    if not root.exists() and not root.is_symlink():
        return None
    with SharedFiles(root) as shared:
        encoded = shared.read(relative, max_bytes=MAX_STATUS_BYTES)
    return json.loads(encoded) if encoded is not None else None


def _valid_commit(value: Any) -> bool:
    """Recognize a full immutable release commit."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _peer_commit(config: dict[str, Any], gate: Gate, peer: str, manifest: dict[str, Any]) -> str:
    """Validate one explicit reader-capability announcement."""
    status = shared_json(config, f"machines/{peer}.json")
    if (not isinstance(status, dict) or status.get("machineId") != peer
            or status.get("protocolVersion") != 2 or not _valid_commit(status.get("runtimeCommit"))
            or not isinstance(status.get("capabilities"), list) or gate.capability not in status["capabilities"]):
        raise ValueError(f"{gate.name} reader upgrade and announcement required")
    if peer == config.get("machineId") and (status["runtimeCommit"] != manifest.get("commit")
                                            or gate.capability not in manifest.get("capabilities", [])):
        raise ValueError(f"announcement does not match the installed {gate.name} runtime")
    return status["runtimeCommit"]


def readiness(config: dict[str, Any], gate: Gate) -> dict[str, Any]:
    """Report all blockers without treating a missing peer as upgraded."""
    blockers, commits = [], {}
    try:
        peers = participants(config, gate)
    except ValueError as error:
        return {"ready": False, "blockers": [str(error)], "runtimeCommits": {}}
    manifest = runtime_manifest(config)
    for peer in peers:
        try:
            commits[peer] = _peer_commit(config, gate, peer, manifest)
        except (OSError, ValueError, TypeError) as error:
            blockers.append(f"{peer}: {error}")
    return {"ready": not blockers, "blockers": blockers, "runtimeCommits": commits}


def require_ready(config: dict[str, Any], gate: Gate) -> dict[str, Any]:
    """Fail before model work or publication if any declared reader is incompatible."""
    status = readiness(config, gate)
    if not status["ready"]:
        raise ValueError(f"{gate.name} blocked: " + "; ".join(status["blockers"]))
    return status


def policy(config: dict[str, Any], gate: Gate) -> dict[str, Any] | None:
    """Validate the shared activation contract and its frozen participant set."""
    value = shared_json(config, gate.policy_path)
    if value is None:
        return None
    peers = participants(config, gate)
    keys = {"version", gate.field, "participants", "runtimeCommits", "activatedAt"}
    if (not isinstance(value, dict) or set(value) != keys or value["version"] != 1
            or type(value["version"]) is not int or value[gate.field] != gate.value or value["participants"] != peers
            or not isinstance(value["runtimeCommits"], dict) or set(value["runtimeCommits"]) != set(peers)
            or not all(_valid_commit(commit) for commit in value["runtimeCommits"].values())):
        raise ValueError(f"invalid shared {gate.name} policy")
    _activation_time(gate, value["activatedAt"])
    return value


def _activation_time(gate: Gate, value: Any) -> None:
    """Require a timezone-qualified timestamp in the immutable activation receipt."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"invalid {gate.name} activation time")
    if datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError(f"{gate.name} activation time requires a timezone")


def activate(config: dict[str, Any], gate: Gate, at: str, apply: bool = False) -> dict[str, Any]:
    """Preview or atomically enable the gate after all reader attestations."""
    status = require_ready(config, gate)
    existing = policy(config, gate)
    if existing is not None:
        return {"status": "enabled", "policy": existing, **status}
    _activation_time(gate, at)
    value = {"version": 1, gate.field: gate.value, "participants": participants(config, gate),
             "runtimeCommits": status["runtimeCommits"], "activatedAt": at}
    if apply:
        encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()
        with SharedFiles(Path(config["exchange"]["root"])) as shared:
            shared.update(gate.policy_path, encoded, None, hashlib.sha256(encoded).hexdigest()[:32],
                          max_bytes=MAX_STATUS_BYTES)
    return {"status": "enabled" if apply else "ready", "policy": value, **status}
