"""Bounded cleanup of private, reproducible replica generations."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from common import load_json, save_json

GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_BATCHES = {"claimed", "retry", "sync-retry", "result-ready", "finalize-retry"}
ERROR_FILE = Path("replica-errors") / "generation-cleanup.json"


def _safe_id(value: Any, generations: Path) -> str | None:
    if not isinstance(value, (str, Path)):
        return None
    try:
        resolved = Path(value).resolve(strict=True)
        if resolved.parent != generations.resolve() or not GENERATION_ID.fullmatch(resolved.name):
            return None
        return resolved.name
    except (OSError, ValueError):
        return None


def _audit_refs(state: Path, generations: Path) -> tuple[set[str], str | None]:
    keep: set[str] = set()
    for path in sorted((state / "batches").glob("*.json")):
        try:
            audit = load_json(path)
        except (OSError, ValueError, TypeError):
            return keep, f"unreadable audit {path.name}"
        if not isinstance(audit, dict):
            return keep, f"invalid audit {path.name}"
        if audit.get("status") not in ACTIVE_BATCHES or "replicaBasis" not in audit:
            continue
        basis = audit.get("replicaBasis")
        if not isinstance(basis, dict) or "wikiRoot" not in basis:
            return keep, f"invalid replica basis {path.name}"
        identifier = _safe_id(basis.get("wikiRoot"), generations)
        if identifier is None:
            return keep, f"unsafe replica basis {path.name}"
        keep.add(identifier)
    return keep, None


def _record_error(state: Path, message: str) -> None:
    try:
        save_json(state / ERROR_FILE, {"operation": "generation-cleanup", "error": message})
    except (OSError, ValueError, TypeError):
        pass


def _fallback_rollback(children: list[Path], current_id: str) -> str | None:
    """Retain one previous directory when upgrading a status without rollbackRoot."""
    candidates = []
    for child in children:
        if (GENERATION_ID.fullmatch(child.name) and child.name != current_id
                and not child.is_symlink() and child.is_dir()):
            try:
                candidates.append((child.stat().st_mtime, child.name))
            except OSError:
                continue
    return max(candidates)[1] if candidates else None


def _remove_unreferenced(children: list[Path], keep: set[str]) -> tuple[int, str | None]:
    """Delete only direct hash directories that no active reader has pinned."""
    removed, errors = 0, []
    for child in children:
        if not GENERATION_ID.fullmatch(child.name):
            continue
        if child.is_symlink() or not child.is_dir():
            errors.append(f"unsafe generation {child.name}")
            continue
        if child.name in keep:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except (OSError, ValueError) as error:
            errors.append(f"{child.name}: {type(error).__name__}")
    return removed, "; ".join(errors[:8]) if errors else None


def _result(state: Path, keep: set[str], removed: int = 0, error: str | None = None) -> dict[str, Any]:
    """Publish cleanup failures and clear a recovered error without touching current."""
    if error:
        _record_error(state, error)
    else:
        try:
            (state / ERROR_FILE).unlink(missing_ok=True)
        except OSError as failure:
            error = type(failure).__name__
            _record_error(state, error)
    return {"removed": removed, "kept": sorted(keep), **({"error": error} if error else {})}


def cleanup_generations(state: Path, current_root: Any, rollback_root: Any = None) -> dict[str, Any]:
    """Keep current, one rollback, and active audits; caller holds replica.lock."""
    state, generations = Path(state), Path(state) / "replica" / "generations"
    if generations.is_symlink() or (state / "replica").is_symlink():
        return _result(state, set(), error="replica generations path is a symlink")
    current_id = _safe_id(current_root, generations)
    rollback_id = _safe_id(rollback_root, generations)
    if current_id is None or (rollback_root is not None and rollback_id is None):
        return _result(state, set(), error="unsafe current or rollback generation root")
    keep, audit_error = _audit_refs(state, generations)
    keep.add(current_id)
    if rollback_id:
        keep.add(rollback_id)
    if audit_error:
        return _result(state, keep, error=audit_error)
    try:
        children = list(generations.iterdir())
    except (OSError, ValueError) as error:
        return _result(state, keep, error=type(error).__name__)
    if not rollback_id:
        fallback = _fallback_rollback(children, current_id)
        if fallback:
            keep.add(fallback)
    removed, error = _remove_unreferenced(children, keep)
    return _result(state, keep, removed, error)
