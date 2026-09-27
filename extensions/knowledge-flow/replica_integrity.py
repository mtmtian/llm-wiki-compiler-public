"""Seal and verify the immutable file set of one private replica generation.

The seal is written inside the generation before it is exposed as ``current``.
Verification reads every approved file through :class:`SharedFiles` and returns
that exact byte snapshot, so callers do not verify one copy and consume another.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replica_records import safe_relative
from shared_files import SharedFiles


MANIFEST_PATH = ".llmwiki/projection-manifest.json"
RESPONSE_PATH = ".llmwiki/replica-response.json"
MANIFEST_VERSION = 2
_GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAVIGATION = {"wiki/MOC.md", "wiki/index.md"}
_MUTABLE_OPERATION_FILES = {".llmwiki/lock", ".llmwiki/lock.reclaim"}
_TRANSACTION_BACKUP = re.compile(r"^\.llmwiki-preserved-[0-9a-f]{32}-[0-9a-f]{16}\.bak$")


@dataclass(frozen=True)
class VerifiedGeneration:
    """Projection and worker response bytes validated against the same seal."""

    projection: dict[str, bytes]
    response: bytes


def _hash(content: bytes) -> str:
    """Return the SHA-256 identity used in the projection manifest."""
    return hashlib.sha256(content).hexdigest()


def _validate_generation_id(generation_id: str) -> str:
    """Require a safe directory-name identity for the generation."""
    if not isinstance(generation_id, str) or not _GENERATION_ID.fullmatch(generation_id):
        raise ValueError("generation id is not a safe directory name")
    return generation_id


def _collect_tree(directory: Path, generation: Path, paths: set[str]) -> None:
    """Collect every regular consumer file below one generation directory."""
    if directory.is_symlink():
        raise ValueError("generation consumer area cannot be a symlink")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise ValueError("generation consumer area is not a directory")
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("generation consumer file cannot be a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("generation consumer entry is not a regular file")
        paths.add(path.relative_to(generation).as_posix())


def _consumer_paths(generation: Path) -> list[str]:
    """Enumerate immutable generation files, excluding seal operation artifacts."""
    paths: set[str] = set()
    _collect_tree(generation, generation, paths)
    return sorted(path for path in paths if not _is_operational_path(path))


def _is_operational_path(relative: str) -> bool:
    """Exclude seal transaction artifacts that may be replaced during recovery."""
    return relative in {MANIFEST_PATH, *_MUTABLE_OPERATION_FILES} or bool(
        _TRANSACTION_BACKUP.fullmatch(Path(relative).name))


def _is_projection_path(relative: str) -> bool:
    """Keep the shared projection contract limited to promoted Markdown files."""
    return (relative in _NAVIGATION
            or relative.endswith(".md") and (relative.startswith("sources/")
                                               or relative.startswith("wiki/concepts/")))


def _snapshot(generation: Path, files: SharedFiles, paths: list[str], all_paths: list[str]) -> dict[str, bytes]:
    """Read one confined snapshot and detect changes to the complete consumer set."""
    snapshot: dict[str, bytes] = {}
    for relative in paths:
        content = files.read(relative)
        if content is None:
            raise ValueError(f"generation file disappeared during verification: {relative}")
        snapshot[relative] = content
    if _consumer_paths(generation) != all_paths:
        raise ValueError("generation file set changed during verification")
    return snapshot


def _manifest_entries(snapshot: dict[str, bytes]) -> list[dict[str, str]]:
    """Encode a sorted file snapshot as manifest entries."""
    return [{"path": relative, "sha256": _hash(snapshot[relative])}
            for relative in sorted(snapshot)]


def _manifest_value(generation_id: str, response: bytes, projection: dict[str, bytes],
                    consumers: dict[str, bytes]) -> dict[str, Any]:
    """Build the seal from all consumers while exposing a narrow projection set."""
    return {"version": MANIFEST_VERSION, "generationId": generation_id, "responseSha256": _hash(response),
            "files": _manifest_entries(projection), "consumerFiles": _manifest_entries(consumers)}


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    """Serialize manifest metadata deterministically for idempotent sealing."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _manifest_transaction(generation_id: str, payload: bytes) -> str:
    """Derive a stable safe transaction id for creating the seal file."""
    seed = b"generation-manifest:" + generation_id.encode("utf-8") + b":" + payload
    return hashlib.sha256(seed).hexdigest()[:32]


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    """Validate the manifest envelope before comparing it with a snapshot."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as error:
        raise ValueError("projection manifest is invalid") from error
    if not isinstance(value, dict) or set(value) != {"version", "generationId", "responseSha256", "files", "consumerFiles"}:
        raise ValueError("projection manifest schema is invalid")
    if type(value["version"]) is not int or value["version"] != MANIFEST_VERSION:
        raise ValueError("projection manifest schema is invalid")
    if not isinstance(value["generationId"], str) or not _GENERATION_ID.fullmatch(value["generationId"]):
        raise ValueError("projection manifest schema is invalid")
    if not isinstance(value["responseSha256"], str):
        raise ValueError("projection manifest schema is invalid")
    for field in ("files", "consumerFiles"):
        _parse_manifest_entries(value[field])
    return value


def _parse_manifest_entries(value: Any) -> None:
    """Validate manifest paths and hashes before comparing a generation snapshot."""
    if not isinstance(value, list) or value != sorted(value, key=lambda item: item.get("path", "") if isinstance(item, dict) else ""):
        raise ValueError("projection manifest files are invalid")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("projection manifest file entry is invalid")
        if not isinstance(item["path"], str):
            raise ValueError("projection manifest file entry is invalid")
        relative = safe_relative(item["path"])
        if relative in seen or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("projection manifest file entry is invalid")
        seen.add(relative)


def seal_generation(stage: Path, generation_id: str) -> dict[str, Any]:
    """Persist an immutable projection manifest for a completed build stage."""
    generation_id = _validate_generation_id(generation_id)
    stage = Path(stage)
    with SharedFiles(stage) as files:
        all_paths = _consumer_paths(stage)
        consumers = _snapshot(stage, files, all_paths, all_paths)
        response = consumers.get(RESPONSE_PATH)
        if response is None:
            raise ValueError("generation response metadata is missing")
        projection = {relative: content for relative, content in consumers.items() if _is_projection_path(relative)}
        manifest = _manifest_value(generation_id, response, projection, consumers)
        payload = _manifest_bytes(manifest)
        current = files.read(MANIFEST_PATH)
        files.update(MANIFEST_PATH, payload, current, _manifest_transaction(generation_id, payload))
    return manifest


def read_verified_generation(generation: Path) -> VerifiedGeneration:
    """Return the exact projection and response bytes covered by one verified seal."""
    generation = Path(generation)
    with SharedFiles(generation) as files:
        raw_manifest = files.read(MANIFEST_PATH)
        if raw_manifest is None:
            raise ValueError("projection manifest is missing")
        manifest = _parse_manifest(raw_manifest)
        _validate_generation_id(generation.name)
        if generation.name != manifest["generationId"]:
            raise ValueError("projection manifest generation id does not match directory")
        all_paths = _consumer_paths(generation)
        consumers = _snapshot(generation, files, all_paths, all_paths)
        response = consumers.get(RESPONSE_PATH)
        if response is None or _hash(response) != manifest["responseSha256"]:
            raise ValueError("generation response metadata changed")
    projection = {relative: content for relative, content in consumers.items() if _is_projection_path(relative)}
    expected = _manifest_value(generation.name, response, projection, consumers)
    if manifest != expected:
        raise ValueError("generation projection files changed")
    return VerifiedGeneration(projection, response)
