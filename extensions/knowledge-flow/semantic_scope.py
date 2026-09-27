"""Activate shared semantic topics only after every replica can read them.

Source project admission stays unchanged. The shared policy selects the page
organization contract, while runtime manifests and peer announcements attest
reader compatibility before the first semantic publication is accepted.
"""

import datetime
import hashlib
import json
import re
from pathlib import Path

from shared_files import SharedFiles
from replica_records import IDENTITY

CAPABILITY = "semantic-topic-revisions-v1"
POLICY_PATH = "v2/topic-scope.json"
MAX_STATUS_BYTES = 16384


def runtime_manifest(config):
    """Read the installed release manifest, never a claimed configuration flag."""
    path = Path(config.get("worker", "")).parent.parent / "build-manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _participants(config):
    """Require a concrete v2 replica set before consulting shared attestations."""
    settings = config.get("exchange", {})
    peers = settings.get("participants")
    if settings.get("protocolVersion") != 2 or not isinstance(peers, list) or not peers:
        raise ValueError("semantic topics require v2 replica participants")
    if any(not isinstance(peer, str) or not IDENTITY.fullmatch(peer) for peer in peers):
        raise ValueError("invalid semantic replica participant")
    if len(set(peers)) != len(peers) or config.get("machineId") not in peers:
        raise ValueError("semantic replica participants must include this machine exactly once")
    return sorted(peers)


def _shared_json(config, relative):
    """Read bounded shared JSON without following symlinks or hiding corruption."""
    root = Path(config["exchange"]["root"])
    if not root.exists() and not root.is_symlink():
        return None
    with SharedFiles(root) as shared:
        encoded = shared.read(relative, max_bytes=MAX_STATUS_BYTES)
    return json.loads(encoded) if encoded is not None else None


def _valid_commit(value):
    """Recognize a full immutable release commit."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _peer_commit(config, peer, manifest):
    """Validate one explicit reader-capability announcement."""
    status = _shared_json(config, f"machines/{peer}.json")
    if (not isinstance(status, dict) or status.get("machineId") != peer
            or status.get("protocolVersion") != 2 or not _valid_commit(status.get("runtimeCommit"))
            or not isinstance(status.get("capabilities"), list) or CAPABILITY not in status["capabilities"]):
        raise ValueError("semantic reader upgrade and announcement required")
    if peer == config.get("machineId") and (status["runtimeCommit"] != manifest.get("commit")
                                            or CAPABILITY not in manifest.get("capabilities", [])):
        raise ValueError("announcement does not match the installed semantic runtime")
    return status["runtimeCommit"]


def readiness(config):
    """Report all blockers without treating a missing peer as upgraded."""
    blockers, commits = [], {}
    try:
        peers = _participants(config)
    except ValueError as error:
        return {"ready": False, "blockers": [str(error)], "runtimeCommits": {}}
    manifest = runtime_manifest(config)
    for peer in peers:
        try:
            commits[peer] = _peer_commit(config, peer, manifest)
        except (OSError, ValueError, TypeError) as error:
            blockers.append(f"{peer}: {error}")
    return {"ready": not blockers, "blockers": blockers, "runtimeCommits": commits}


def require_ready(config):
    """Fail before model work or publication if any declared reader is incompatible."""
    status = readiness(config)
    if not status["ready"]:
        raise ValueError("semantic topics blocked: " + "; ".join(status["blockers"]))
    return status


def _policy(config):
    """Validate the shared activation contract and its frozen participant set."""
    value = _shared_json(config, POLICY_PATH)
    if value is None:
        return None
    peers = _participants(config)
    keys = {"version", "topicScope", "participants", "runtimeCommits", "activatedAt"}
    if (not isinstance(value, dict) or set(value) != keys or value["version"] != 1
            or type(value["version"]) is not int or value["topicScope"] != "semantic" or value["participants"] != peers
            or not isinstance(value["runtimeCommits"], dict) or set(value["runtimeCommits"]) != set(peers)
            or not all(_valid_commit(commit) for commit in value["runtimeCommits"].values())):
        raise ValueError("invalid shared semantic topic policy")
    _activation_time(value["activatedAt"])
    return value


def _activation_time(value):
    """Require a timezone-qualified timestamp in the immutable activation receipt."""
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid semantic activation time")
    if datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("semantic activation time requires a timezone")


def apply_scope(config):
    """Derive effective scope solely from the shared policy, not a local toggle."""
    result = {key: value for key, value in config.items() if key != "topicScope"}
    if config.get("exchange", {}).get("protocolVersion") == 2 and _policy(config) is not None:
        result["topicScope"] = "semantic"
    return result


def activate(config, at, apply=False):
    """Preview or atomically enable semantic topics after all reader attestations."""
    status = require_ready(config)
    existing = _policy(config)
    if existing is not None:
        return {"status": "enabled", "policy": existing, **status}
    _activation_time(at)
    policy = {"version": 1, "topicScope": "semantic", "participants": _participants(config),
              "runtimeCommits": status["runtimeCommits"], "activatedAt": at}
    if apply:
        encoded = (json.dumps(policy, sort_keys=True, ensure_ascii=False) + "\n").encode()
        with SharedFiles(Path(config["exchange"]["root"])) as shared:
            shared.update(POLICY_PATH, encoded, None, hashlib.sha256(encoded).hexdigest()[:32],
                          max_bytes=MAX_STATUS_BYTES)
    return {"status": "enabled" if apply else "ready", "policy": policy, **status}


def require_semantic_job(config, job):
    """Keep a queued job's contract frozen and verify activation before processing."""
    if "topicScope" not in job:
        return
    if job["topicScope"] != "semantic" or apply_scope(config).get("topicScope") != "semantic":
        raise ValueError("semantic job requires shared topic activation")
    require_ready(config)


def require_publication(config, job, result):
    """Reject semantic revisions injected through a legacy or unactivated job."""
    revisions = result.get("contribution", {}).get("topicRevisions", [])
    if any(isinstance(revision, dict) and "topicScope" in revision for revision in revisions):
        if job.get("topicScope") != "semantic":
            raise ValueError("semantic revision requires a semantic job")
    if job.get("topicScope") == "semantic" and (not revisions or any(
            not isinstance(revision, dict) or revision.get("topicScope") != "semantic" for revision in revisions)):
        raise ValueError("semantic job requires semantic revisions")
    require_semantic_job(config, job)
