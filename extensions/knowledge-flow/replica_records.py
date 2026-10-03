"""Low-level contracts for the v2 iCloud replica exchange.

The exchange is an append-only set of complete JSON files.  Every machine may
publish a reviewed record, while each machine rebuilds its own local index.
This module contains validation and immutable-file helpers; it never invokes a
model and never writes to the shared Wiki.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import datetime as _datetime
from pathlib import Path
from typing import Any

from common import digest, inside

IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
OPAQUE_LOCATOR = re.compile(r"^knowledge-evidence://([A-Za-z0-9][A-Za-z0-9._-]{0,63})/([a-f0-9]{64})$")
MAX_PACKET_BYTES = 128 * 1024
MAX_BASELINE_BYTES = 64 * 1024 * 1024
MAX_CLAIMS = 5
MAX_EVIDENCE = 20
PUBLICATION_VERSION = 2
LEDGER_VERSION = 3  # claims-only ledger records, deployment/KNOWLEDGE-LEDGER.md §7.1
MAX_SUPERSEDES = 5
CLAIM_REF = re.compile(r"^([a-f0-9]{64}):(0|[1-9][0-9]*)$")
REQUIRED_BASELINE_PREFIXES = ("sources/", "wiki/")
METADATA_PATHS = {".llmwiki/config.json", ".llmwiki/state.json", ".llmwiki/schema.json"}
SECRET_KEY = re.compile(
    r"(?:credential|password|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|auth[_-]?header)", re.I
)


def canonical(value: Any) -> str:
    """Return deterministic JSON used for all content identities."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def is_hash(value: Any) -> bool:
    """Check the lowercase SHA-256 representation used by the protocol."""
    return isinstance(value, str) and bool(HASH.fullmatch(value))


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate v2 paths, participants, and the absence of credential keys."""
    if not isinstance(config, dict):
        raise ValueError("replica config must be an object")
    validate_no_credentials(config)
    exchange = config.get("exchange")
    if not isinstance(exchange, dict) or exchange.get("protocolVersion") != 2:
        raise ValueError("exchange protocolVersion 2 is required")
    if "publisherMachineId" in exchange:
        raise ValueError("v2 exchange must not define a fixed publisher")
    root = exchange.get("root")
    participants = exchange.get("participants")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("exchange.root must be an absolute path")
    if (not isinstance(participants, list) or not participants
            or len(set(participants)) != len(participants)
            or any(not isinstance(item, str) or not IDENTITY.fullmatch(item) for item in participants)):
        raise ValueError("exchange.participants must be unique machine identities")
    if "materializerMachineId" in exchange and exchange["materializerMachineId"] not in participants:
        raise ValueError("exchange.materializerMachineId must be a participant")
    if exchange.get('sharedWriter') is not None:
        from writer_records import settings
        settings(config)
    machine = config.get("machineId")
    if not isinstance(machine, str) or not IDENTITY.fullmatch(machine) or machine not in participants:
        raise ValueError("machineId must be a declared participant")
    importer = exchange.get("legacyImporterMachineId")
    if ("legacyImporterMachineId" in exchange
            and (not isinstance(importer, str) or not IDENTITY.fullmatch(importer)
                 or importer not in participants)):
        raise ValueError("legacyImporterMachineId must be a participant")
    if config.get("intakeEnabled") and not config.get("publishEnabled") and importer is None:
        raise ValueError("v2 contributor requires legacyImporterMachineId")
    require_absolute(config, "sharedWikiRoot")
    require_absolute(config, "stateDir")
    require_absolute(config, "wikiRoot")
    reject_shared_wiki_path(config, Path(config["stateDir"]))
    reject_shared_wiki_path(config, Path(config["wikiRoot"]))
    expected = Path(config["stateDir"]) / "replica" / "current"
    if Path(config["wikiRoot"]) != expected:
        raise ValueError("wikiRoot must be stateDir/replica/current")
    return exchange


def validate_no_credentials(value: Any) -> None:
    """Reject credential-shaped mapping keys before config is persisted or used."""
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(str(key)):
                raise ValueError("replica config contains a credential key")
            validate_no_credentials(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            validate_no_credentials(child)


def require_absolute(config: dict[str, Any], key: str) -> None:
    """Require one of the machine-local filesystem roots."""
    value = config.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"{key} must be an absolute path")


def reject_shared_wiki_path(config: dict[str, Any], wiki_root: Path) -> None:
    """Prevent a local generation view from pointing into shared source data."""
    shared = Path(config["sharedWikiRoot"])
    exchange = Path(config["exchange"]["root"])
    for label, target in (("shared Wiki", shared), ("exchange", exchange)):
        if inside(wiki_root, target) or inside(target, wiki_root):
            raise ValueError(f"wikiRoot cannot overlap {label}")


def safe_relative(path: str) -> str:
    """Normalize a relative POSIX path and reject traversal or absolute names."""
    candidate = Path(path)
    if candidate.is_absolute() or "\\" in path or "\x00" in path:
        raise ValueError("exchange path is not relative")
    normalized = candidate.as_posix()
    if normalized in ("", ".") or normalized.startswith("../") or "/../" in f"/{normalized}":
        raise ValueError("exchange path escapes its root")
    return normalized


def baseline_path_allowed(path: str) -> bool:
    """Limit baseline content to approved source, wiki, and metadata files."""
    normalized = safe_relative(path)
    if normalized in METADATA_PATHS:
        return True
    return normalized.startswith(REQUIRED_BASELINE_PREFIXES)


def immutable_write(path: Path, content: bytes) -> None:
    """Atomically expose a complete immutable file and reject conflicting bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("immutable exchange path cannot be a symlink")
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("immutable exchange file changed")
        return
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError("conflicting immutable exchange write")
    finally:
        Path(temporary).unlink(missing_ok=True)


def immutable_json(path: Path, value: Any) -> None:
    """Write canonical JSON with one terminal newline through immutable_write."""
    immutable_write(path, (canonical(value) + "\n").encode("utf-8"))


def load_json(path: Path, max_bytes: int) -> Any:
    """Parse one complete UTF-8 JSON file under a caller-supplied size bound."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("invalid or oversized replica file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid replica JSON") from error


def validate_baseline(manifest: Any) -> dict[str, Any]:
    """Validate baseline shape, every text hash, and its content-addressed id."""
    if not isinstance(manifest, dict) or manifest.get("version") != 2:
        raise ValueError("unknown baseline version")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("baseline files must be sorted")
    try:
        ordered = sorted(files, key=lambda item: item.get("path", "") if isinstance(item, dict) else "")
    except (TypeError, AttributeError):
        raise ValueError("baseline files must be sorted") from None
    if files != ordered:
        raise ValueError("baseline files must be sorted")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "text", "sha256"}:
            raise ValueError("invalid baseline file entry")
        path = safe_relative(item["path"])
        if path in seen or not baseline_path_allowed(path) or not isinstance(item["text"], str) or not is_hash(item["sha256"]):
            raise ValueError("invalid baseline file entry")
        if digest(item["text"]) != item["sha256"]:
            raise ValueError("baseline file hash mismatch")
        if path in METADATA_PATHS:
            _validate_metadata(item["text"])
        seen.add(path)
        normalized.append({"path": path, "text": item["text"], "sha256": item["sha256"]})
    snapshot = digest(canonical(normalized))
    if manifest.get("snapshotId") != snapshot:
        raise ValueError("baseline snapshot hash mismatch")
    return {"version": 2, "snapshotId": snapshot, "files": normalized}


def _validate_metadata(text: str) -> None:
    """Ensure copied compiler metadata cannot carry credential fields."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("baseline metadata must be valid JSON") from error
    validate_no_credentials(value)


def validate_packet(packet: Any, machine: str, baseline_id: str) -> dict[str, Any]:
    """Validate publication identity, review status, quotes, and evidence hashes."""
    if not isinstance(packet, dict) or set(packet) != {"id", "payload"} or not is_hash(packet.get("id")):
        raise ValueError("invalid publication packet")
    payload = packet.get("payload")
    if not isinstance(payload, dict) or payload.get("version") not in (PUBLICATION_VERSION, LEDGER_VERSION):
        raise ValueError("unknown publication version")
    required = {"version", "baselineId", "machineId", "projectId", "projectLabel", "createdAt",
                "originJobHash", "repoIdentity", "basisRecordIds", "claims", "evidence", "review"}
    if not required.issubset(payload) or payload["baselineId"] != baseline_id or payload["machineId"] != machine:
        raise ValueError("publication baseline or participant mismatch")
    if digest(canonical(payload)) != packet["id"]:
        raise ValueError("publication hash mismatch")
    _validate_packet_metadata(payload)
    claims, evidence = payload.get("claims"), payload.get("evidence")
    if (not isinstance(claims, list) or not 1 <= len(claims) <= MAX_CLAIMS
            or not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE):
        raise ValueError("publication claim limit or evidence shape is invalid")
    evidence_by_id = validate_evidence(evidence, machine)
    if not valid_evidence_roles(claims, evidence):
        raise ValueError("assistant evidence requires historical lessons")
    _validate_claims(claims, evidence_by_id)
    _validate_ledger(payload)
    _validate_topic_revisions(payload.get("topicRevisions"), claims, payload.get("projectId"))
    _validate_basis(payload.get("basisRecordIds"))
    return packet


def _validate_packet_metadata(payload: dict[str, Any]) -> None:
    """Validate identity, timestamp, and independent-review metadata."""
    if (not isinstance(payload.get("projectId"), str) or not payload["projectId"]
            or not isinstance(payload.get("projectLabel"), str) or not payload["projectLabel"]
            or not isinstance(payload.get("createdAt"), str) or not payload["createdAt"]
            or not is_hash(payload.get("originJobHash"))
            or payload.get("repoIdentity") is not None and not isinstance(payload.get("repoIdentity"), str)):
        raise ValueError("publication metadata is invalid")
    try:
        _datetime.datetime.fromisoformat(payload["createdAt"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("publication timestamp is invalid") from None
    review = payload.get("review")
    if not isinstance(review, dict) or review.get("status") != "accepted" or not isinstance(review.get("model"), str):
        raise ValueError("publication is not independently accepted")


def _validate_claims(claims: list[Any], evidence_by_id: dict[str, dict[str, Any]]) -> None:
    """Validate exact quotes and reject duplicate or human-review claims."""
    claim_texts: set[str] = set()
    used_evidence: set[str] = set()
    for claim in claims:
        if (not isinstance(claim, dict) or not isinstance(claim.get("evidenceId"), str)
                or not isinstance(claim.get("quote"), str) or not isinstance(claim.get("text"), str)):
            raise ValueError("publication claim evidence is invalid")
        item = evidence_by_id.get(claim["evidenceId"])
        if item is None or claim.get("quote") != item["text"]:
            raise ValueError("publication quote does not match evidence")
        used_evidence.add(claim["evidenceId"])
        _validate_optional_claim_fields(claim)
        for support in claim.get("supportingQuotes", []):
            if (not isinstance(support, dict) or set(support) != {"evidenceId", "quote"}
                    or support.get("evidenceId") not in evidence_by_id
                    or support.get("quote") != evidence_by_id[support["evidenceId"]]["text"]):
                raise ValueError("publication supporting quote is invalid")
            used_evidence.add(support["evidenceId"])
        if claim["text"] in claim_texts or claim["status"] == "uncertain" or claim.get("replacementIntent") is True:
            raise ValueError("publication claim requires human review")
        if not any(character.isalnum() for character in claim["slug"]):
            raise ValueError("publication claim slug is invalid")
        claim_texts.add(claim["text"])
    if len(used_evidence) != len(evidence_by_id):
        raise ValueError("publication evidence is not referenced by a claim")


def _validate_basis(basis: Any) -> None:
    """Require a bounded, typed, duplicate-free set of observed packet ids."""
    if not isinstance(basis, list) or len(basis) > 1024 or any(not isinstance(item, str) for item in basis):
        raise ValueError("publication basisRecordIds is invalid")
    if len(set(basis)) != len(basis) or any(not is_hash(item) for item in basis):
        raise ValueError("publication basisRecordIds is invalid")


def _validate_ledger(payload: dict[str, Any]) -> None:
    """Ledger records carry claims only; only their decided claims with a subject may supersede.

    Rules that need other records (target visible, same subject, earlier) live in ``ledger.py``.
    """
    is_ledger = payload["version"] == LEDGER_VERSION
    if is_ledger and "topicRevisions" in payload:
        raise ValueError("ledger record cannot carry topic revisions")
    for claim in payload["claims"]:
        refs = claim.get("supersedes")
        if "supersedes" in claim and (
                not is_ledger or claim["status"] != "decided" or "decisionObject" not in claim
                or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_SUPERSEDES
                or any(not isinstance(ref, str) or not CLAIM_REF.fullmatch(ref) for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError("publication claim supersedes is invalid")


def _validate_optional_claim_fields(claim: dict[str, Any]) -> None:
    """Require the FlowClaim fields consumed by the materializer."""
    limits = {"text": 1200, "title": 160, "topic": 160, "slug": 100, "useWhen": 500, "rationale": 500}
    fields = set(limits) | {"evidenceId", "quote", "targetPageId", "kind", "status"}
    if not fields.issubset(claim) or any(not isinstance(claim[field], str) or not claim[field].strip()
                                         or len(claim[field]) > limits.get(field, 600)
                                         for field in fields - {"targetPageId"}):
        raise ValueError("publication claim field is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", claim["evidenceId"]):
        raise ValueError("publication claim evidence id is invalid")
    if len(claim["quote"]) > 600 or "\r" in claim["quote"]:
        raise ValueError("publication claim quote is invalid")
    if claim["targetPageId"] is not None:
        target = claim["targetPageId"]
        component = target.split("/", 1)[1] if isinstance(target, str) and target.count("/") == 1 else ""
        if (not isinstance(target, str) or "\x00" in target or target.count("/") != 1
                or not target.startswith("concepts/") or component in ("", ".", "..")
                or component.startswith(".") or any(character in component for character in "/\\ \x00")
                or any(ord(character) < 32 or ord(character) == 127 for character in component)):
            raise ValueError("publication claim target is invalid")
    if claim["kind"] not in ("decision", "fact", "constraint", "lesson") or claim["status"] not in ("decided", "historical", "uncertain"):
        raise ValueError("publication claim classification is invalid")
    if "replacementIntent" in claim and not isinstance(claim["replacementIntent"], bool):
        raise ValueError("publication claim replacement flag is invalid")
    if "decisionObject" in claim:
        decision_object = claim["decisionObject"]
        if (not isinstance(decision_object, str) or not decision_object.strip()
                or len(decision_object) > 160):
            raise ValueError("publication claim decisionObject is invalid")
    supporting = claim.get("supportingQuotes", [])
    if (not isinstance(supporting, list) or len(supporting) > 3
            or any(not isinstance(item, dict) for item in supporting)):
        raise ValueError("publication supporting quote limit is invalid")


def validate_evidence(items: list[Any], machine: str) -> dict[str, dict[str, Any]]:
    """Validate quote-only evidence and enforce opaque machine-local locators."""
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "text", "sha256", "originalSha256", "observedAt", "locator"}:
            raise ValueError("publication evidence fields are invalid")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ValueError("publication evidence id is invalid")
        if item.get("kind") not in ("user", "artifact", "assistant") or not isinstance(item.get("text"), str):
            raise ValueError("assistant or invalid publication evidence")
        if not is_hash(item.get("sha256")) or digest(item["text"]) != item["sha256"] or not is_hash(item.get("originalSha256")):
            raise ValueError("publication evidence hash mismatch")
        if not isinstance(item.get("observedAt"), str) or not item["observedAt"]:
            raise ValueError("publication evidence timestamp is invalid")
        match = OPAQUE_LOCATOR.fullmatch(str(item.get("locator")))
        if not match or match.group(1) != machine:
            raise ValueError("publication evidence locator is not opaque")
        result[identifier] = item
    return result


def valid_evidence_roles(claims, evidence):
    """Keep assistant primaries historical while allowing quoted context support."""
    by_id = {item.get("id"): item for item in evidence if isinstance(item, dict)}
    for item in evidence:
        if not isinstance(item, dict):
            return False
        if item.get("kind") in ("user", "artifact"):
            continue
        primary = [claim for claim in claims if isinstance(claim, dict)
                   and claim.get("evidenceId") == item.get("id")]
        supporting = [claim for claim in claims if isinstance(claim, dict)
                      and any(isinstance(support, dict) and support.get("evidenceId") == item.get("id")
                              for support in claim.get("supportingQuotes", []))]
        if item.get("kind") != "assistant" or not primary and not supporting:
            return False
        if any(claim.get("kind") != "lesson" or claim.get("status") != "historical" for claim in primary):
            return False
        if any(by_id.get(claim.get("evidenceId"), {}).get("kind") != "user" for claim in supporting):
            return False
    for claim in claims:
        if (isinstance(claim, dict) and claim.get("status") == "decided"
                and isinstance(claim.get("evidenceId"), str)):
            primary = next((item for item in evidence if isinstance(item, dict)
                            and item.get("id") == claim["evidenceId"]), None)
            if not primary or primary.get("kind") != "user":
                return False
    return True


def _validate_topic_revisions(value: Any, claims: list[Any], project_id: str | None = None) -> None:
    """Validate optional complete page revisions without importing the TS layer."""
    if value is None:
        return
    from revision_contract import validate_topic_revisions
    validate_topic_revisions(value, claims, project_id)
