"""Exchange reviewed records and migrate legacy multi-machine proposals.

Protocol v2 participants publish independently and rebuild compiled state on
each machine. Legacy v1 submissions retain their original receipt contract.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from common import digest, inside, load_json, page_ids, save_json
from routing import eligible_repo
from replica_records import valid_evidence_roles

MAX_PACKET_BYTES = 64000
IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HASH = re.compile(r"^[a-f0-9]{64}$")


class LegacyImporterError(ValueError):
    """Raised when a legacy v1 proposal reaches a non-importer machine."""


def legacy_importer(exchange):
    """Validate and return the sole v2 legacy importer, or None when disabled."""
    if "legacyImporterMachineId" not in exchange:
        return None
    value = exchange.get("legacyImporterMachineId")
    members = exchange.get("participants", [])
    if (not isinstance(value, str) or not IDENTITY.fullmatch(value)
            or not isinstance(members, list) or value not in members):
        raise ValueError("exchange legacyImporterMachineId must be a participant")
    return value


def require_legacy_importer(config, job):
    """Reject a legacy exchange queue before model work or v2 publication."""
    exchange = config.get("exchange") or {}
    if exchange.get("protocolVersion") != 2 or not str(job.get("id", "")).startswith("exchange-"):
        return
    importer = legacy_importer(exchange)
    if importer != config.get("machineId"):
        raise LegacyImporterError("legacy exchange job requires designated importer")


def settings(config):
    """Reject ambiguous roles instead of falling back to a second writer."""
    value = config.get("exchange")
    if not value:
        return None
    machine = config.get("machineId", "")
    publisher = value.get("publisherMachineId", "")
    members = value.get("participants", [])
    importer = legacy_importer(value)
    if value.get("protocolVersion") == 2:
        if (not isinstance(members, list) or not members
                or not all(isinstance(item, str) and IDENTITY.fullmatch(item) for item in members)
                or len(members) != len(set(members))
                or machine not in members or not Path(value.get("root", "")).is_absolute()):
            raise ValueError("Invalid immutable publication configuration")
        if config.get("intakeEnabled") and not config.get("publishEnabled") and importer is None:
            raise ValueError("v2 contributor requires legacyImporterMachineId")
        return value
    if (not isinstance(members, list) or not members
            or not all(isinstance(item, str) and IDENTITY.fullmatch(item) for item in members)
            or machine not in members or publisher not in members
            or not Path(value.get("root", "")).is_absolute()):
        raise ValueError("Invalid knowledge exchange configuration")
    if config.get("publishEnabled") and machine != publisher:
        raise ValueError("Only the designated machine can publish")
    return value


def canonical(value):
    """Stable UTF-8 JSON identity independent of file ordering or local paths."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def immutable_write(path, value):
    """Expose a whole immutable file; a conflicting existing value is an error."""
    path = Path(path)
    content = canonical(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.is_symlink() or path.read_text(encoding="utf-8") != content:
            raise ValueError("Immutable exchange file changed")
        return
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise ValueError("Conflicting exchange write")
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_result(config, job, result):
    """Export reviewed quote-only knowledge, leaving complete evidence private."""
    exchange = settings(config)
    if result.get("status") != "submitted":
        return result
    if exchange and exchange.get("protocolVersion") == 2 and config.get("publishEnabled"):
        from replica import publish_record
        return publish_record(config, job, result)
    if not exchange:
        raise ValueError("Submitted result requires exchange")
    contribution = result["contribution"]
    evidence = [shared_evidence(item, config["machineId"]) for item in contribution["evidence"]]
    payload = {"version": 1, "machineId": config["machineId"], "projectId": job["projectId"],
               "projectLabel": job["projectLabel"], "createdAt": job["createdAt"],
               "originJobHash": digest(job["id"]), "repoIdentity": job.get("repoIdentity"),
               "claims": contribution["claims"], "evidence": evidence}
    packet = {"id": digest(canonical(payload)), "payload": payload}
    if len(canonical(packet).encode("utf-8")) > MAX_PACKET_BYTES:
        raise ValueError("Knowledge proposal exceeds exchange limit")
    path = Path(exchange["root"]) / "submissions" / config["machineId"] / (packet["id"] + ".json")
    immutable_write(path, packet)
    return {key: value for key, value in result.items() if key != "contribution"} | {"submissionId": packet["id"]}


def shared_evidence(item, machine):
    """Use an opaque locator; the original locator remains in the local audit."""
    result = {key: item[key] for key in ("id", "kind", "text", "sha256")}
    result["observedAt"] = item.get("observedAt", "")
    result["originalSha256"] = item.get("originalSha256", item["sha256"])
    result["locator"] = "knowledge-evidence://" + machine + "/" + digest(item.get("locator", ""))
    return result


def read_packet(path, machine, exchange):
    """Validate a complete packet and its transport hash before trusting fields."""
    if path.is_symlink() or not inside(path, exchange["root"]) or path.stat().st_size > MAX_PACKET_BYTES:
        raise ValueError("Invalid exchange file")
    packet = load_json(path)
    payload = packet["payload"]
    if (not HASH.fullmatch(packet["id"]) or packet["id"] != path.stem
            or digest(canonical(payload)) != packet["id"] or payload.get("version") != 1
            or payload.get("machineId") != machine):
        raise ValueError("Knowledge proposal integrity failure")
    claims, evidence = payload.get("claims"), payload.get("evidence")
    if not isinstance(claims, list) or not 1 <= len(claims) <= 5 or not isinstance(evidence, list):
        raise ValueError("Invalid shared claims")
    if len(evidence) != len(claims) or not valid_evidence_roles(claims, evidence):
        raise ValueError("Invalid shared evidence")
    return packet


def project_allowed(payload, config, allow_archived=False):
    """Re-establish publisher-side project ownership; never accept sender paths."""
    project = payload.get("projectId")
    if project in config["projects"]:
        return True
    identity = payload.get("repoIdentity")
    return (isinstance(identity, str) and project == "repo-" + identity.replace("/", "-")
            and eligible_repo(identity, config, allow_archived=allow_archived))


def incoming_job(packet, config):
    """Convert a shared quote proposal into a locally scoped, re-reviewed job."""
    payload = packet["payload"]
    if not project_allowed(payload, config):
        raise ValueError("Shared proposal project is not allowed")
    return {"id": "exchange-" + packet["id"], "projectId": payload["projectId"],
            "projectLabel": config["projects"].get(payload["projectId"], {}).get("label", payload["projectLabel"]),
            "cwd": config["wikiRoot"], "createdAt": payload["createdAt"],
            "sessionId": "exchange-" + payload["machineId"], "turnId": packet["id"],
            "repoIdentity": payload.get("repoIdentity"),
            "prompt": "Review shared project knowledge against current accepted pages.", "lastAssistant": "",
            "evidence": payload["evidence"], "submittedClaims": payload["claims"],
            "allowedPageIds": page_ids(config, payload["projectId"])}


def import_pending(config):
    """Queue a bounded set only on the designated publisher; receipts prevent replay."""
    exchange = settings(config)
    if not exchange or not config.get("publishEnabled") or not config.get("intakeEnabled"):
        return 0
    if exchange.get("protocolVersion") == 2 and legacy_importer(exchange) != config.get("machineId"):
        return 0
    state = Path(config["stateDir"])
    capacity = max(0, config.get("maxQueuedJobs", 30) - len(list((state / "queue").glob("*.json"))))
    imported = 0
    for machine in exchange["participants"]:
        folder = Path(exchange["root"]) / "submissions" / machine
        for path in sorted(folder.glob("*.json")):
            if imported >= capacity:
                return imported
            try:
                packet = read_packet(path, machine, exchange)
                imported += queue_packet(packet, config)
                (state / "exchange-errors" / (digest(str(path)) + ".json")).unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                save_json(state / "exchange-errors" / (digest(str(path)) + ".json"), {"file": path.name, "error": "InvalidProposal"})
    return imported


def queue_packet(packet, config):
    """Use a packet identity shared by local queues, audits, and publisher receipts."""
    root = Path(config["exchange"]["root"])
    state = Path(config["stateDir"])
    identifier = "exchange-" + packet["id"]
    if config["exchange"].get("protocolVersion") == 2:
        from replica import read_records
        if any(record["payload"]["originJobHash"] == digest(identifier) for record in read_records(config)):
            return 0
    if valid_receipt(root / "receipts" / (packet["id"] + ".json"), packet["id"], config):
        return 0
    completed = load_json(state / "completed" / (identifier + ".json"))
    if completed:
        write_receipt(config, {"id": identifier}, completed)
        return 0
    if (state / "queue" / (identifier + ".json")).exists() or (state / "failed" / (identifier + ".json")).exists():
        return 0
    save_json(state / "queue" / (identifier + ".json"), incoming_job(packet, config))
    return 1


def valid_receipt(path, identifier, config):
    """A partial cloud file cannot suppress a pending contribution."""
    if not path.exists():
        return False
    if path.is_symlink() or path.stat().st_size > MAX_PACKET_BYTES:
        raise ValueError("Invalid publisher receipt")
    value = load_json(path)
    publishers = config["exchange"]["participants"] if config["exchange"].get("protocolVersion") == 2 else [config["exchange"]["publisherMachineId"]]
    if (not isinstance(value, dict) or value.get("submissionId") != identifier
            or value.get("publisherMachineId") not in publishers
            or value.get("status") not in ("published", "empty", "needs_review")
            or not isinstance(value.get("publishedPageIds"), list)):
        raise ValueError("Invalid publisher receipt")
    return True


def write_receipt(config, job, result):
    """Only the publisher writes sanitized receipts; private review paths stay local."""
    exchange = settings(config)
    if exchange and exchange.get("protocolVersion") == 2:
        return
    if (not exchange or not config.get("publishEnabled") or not job["id"].startswith("exchange-")
            or result.get("status") not in ("published", "empty", "needs_review")):
        return
    identifier = job["id"][len("exchange-"):]
    if not HASH.fullmatch(identifier):
        raise ValueError("Invalid receipt identity")
    receipt = {"submissionId": identifier, "publisherMachineId": config["machineId"],
               "status": result["status"], "publishedPageIds": result.get("publishedPageIds", []),
               "reviewCount": result.get("reviewCount", 0)}
    immutable_write(Path(exchange["root"]) / "receipts" / (identifier + ".json"), receipt)


def exchange_counts(config):
    """Inspect only this role's submissions and their corresponding receipts."""
    exchange = settings(config)
    if not exchange or exchange.get("protocolVersion") == 2:
        return {"submitted": 0, "unreceipted": 0}
    root = Path(exchange["root"])
    members = exchange["participants"] if config.get("publishEnabled") else [config["machineId"]]
    submissions = [path for machine in members
                   for path in (root / "submissions" / machine).glob("*.json")]
    missing = sum(not (root / "receipts" / (path.stem + ".json")).is_file() for path in submissions)
    return {"submitted": len(submissions), "unreceipted": missing}


def exchange_status(config):
    """Report peer readiness explicitly; automatic wakes only need role-scoped counts."""
    exchange = settings(config)
    if not exchange:
        return None
    if exchange.get("protocolVersion") == 2:
        from replica import replica_status
        status = replica_status(config)
        status["peers"] = peer_status(exchange)
        status["role"] = "writer" if config.get("publishEnabled") else "contributor" if config.get("intakeEnabled") else "reader"
        return status
    return {"machineId": config["machineId"], "publisherMachineId": exchange["publisherMachineId"],
            "role": "publisher" if config.get("publishEnabled") else "contributor" if config.get("intakeEnabled") else "reader",
            **exchange_counts(config), "peers": peer_status(exchange)}


def peer_status(exchange):
    """A partly synchronized peer announcement does not block local maintenance."""
    peers = {}
    for machine in exchange["participants"]:
        path = Path(exchange["root"]) / "machines" / (machine + ".json")
        try:
            value = load_json(path, {})
            if not isinstance(value, dict):
                raise ValueError("Invalid machine status")
            peers[machine] = value
        except (OSError, ValueError):
            peers[machine] = {"error": "UnreadableMachineStatus"}
    return peers


def announce_machine(config, at):
    """Write only this machine's status, not another host's configuration."""
    exchange = settings(config)
    if not exchange:
        return
    status = {"machineId": config["machineId"], "protocolVersion": exchange.get("protocolVersion", 1),
              "enabled": bool(config.get("enabled")), "intakeEnabled": bool(config.get("intakeEnabled")),
              "publishEnabled": bool(config.get("publishEnabled")), "checkedAt": at}
    if exchange.get("protocolVersion") != 2:
        status["publisherMachineId"] = exchange["publisherMachineId"]
    from capability_gate import runtime_manifest
    manifest = runtime_manifest(config)
    status["runtimeCommit"] = manifest.get("commit")
    status["capabilities"] = manifest.get("capabilities", [])
    from writer_records import settings as writer_settings
    if writer_settings(config) is not None:
        status['sharedWriter'] = writer_settings(config)
    save_json(Path(exchange["root"]) / "machines" / (config["machineId"] + ".json"), status)
