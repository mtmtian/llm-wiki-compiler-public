"""Private, reversible filesystem operations for the host installer.

The installer first snapshots and stages every changed file, then compares
the snapshots again before replacing any destination. Immutable bundle pointer
changes are atomic and validated against the same content-addressed manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

Snapshot = tuple[bool, bytes | None, int | None]
FilePlan = list[tuple[Path, str, int]]
RUNTIME_FILES = frozenset(("bridge.py", "claude_adapter.py", "claude_capture.py", "pi-extension.ts"))


class PlannedWrites(list[tuple[Path, str, int]]):
    """List-compatible file plan with all planning-time read snapshots."""

    def __init__(self, *args: Any, read_set: dict[Path, Snapshot] | None = None,
                 current: tuple[Path, str | None] | None = None) -> None:
        super().__init__(*args)
        self.read_set = read_set if read_set is not None else {}
        self.current_expected = current


def snapshot(path: Path) -> Snapshot:
    """Capture one file while refusing symlinks and non-file occupants."""
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    if not path.exists():
        return False, None, None
    if not path.is_file():
        raise ValueError(f"refusing to replace non-file: {path}")
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def remember_read(read_set: dict[Path, Snapshot], path: Path) -> None:
    """Capture an input before parsing it so later edits invalidate the plan."""
    target = Path(os.path.abspath(path))
    if target not in read_set:
        read_set[target] = snapshot(target)


def check_parent_chain(path: Path) -> None:
    """Reject symlinked parent directories before creating or replacing files."""
    parent = path.parent
    missing: list[Path] = []
    while not parent.exists():
        missing.append(parent)
        if parent.parent == parent:
            break
        parent = parent.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError(f"unsafe parent directory: {parent}")
    for candidate in missing:
        if candidate.is_symlink():
            raise ValueError(f"unsafe parent directory: {candidate}")


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Replace one file atomically using a same-directory private temporary."""
    check_parent_chain(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-plugin-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def backup_path(path: Path, home: Path, backup: Path, index: int) -> Path:
    """Map changed targets to private backup files without exposing contents."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        relative = Path("_external") / hashlib.sha256(str(path).encode()).hexdigest()[:16] / path.name
    return backup / relative.parent / f"{index}-{relative.name}"


def backup_changes(changes: list[tuple[Path, bytes, int]], initial: dict[Path, Snapshot],
                   home: Path) -> Path | None:
    """Store old bytes and a path manifest before any planned write."""
    if not changes:
        return None
    backup = home / ".local/share/llm-wiki-compiler/install-backups/agent-plugins"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup / stamp
    check_parent_chain(backup)
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = []
    for index, (path, _, _) in enumerate(changes):
        exists, raw, mode = initial[path]
        manifest.append({"path": str(path), "existed": exists, "mode": mode})
        if exists and raw is not None:
            destination = backup_path(path, home, backup, index)
            atomic_write(destination, raw, mode or 0o600)
    atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    return backup


def restore_files(initial: dict[Path, Snapshot], touched: list[Path],
                  expected_after: dict[Path, Snapshot]) -> None:
    """Restore only files already replaced by this transaction."""
    for path in reversed(touched):
        if snapshot(path) != expected_after[path]:
            raise RuntimeError(f"target changed after install; refusing rollback: {path}")
        exists, raw, mode = initial[path]
        if exists and raw is not None:
            atomic_write(path, raw, mode or 0o600)
        else:
            path.unlink(missing_ok=True)


def write_file_plan(plan: FilePlan, home: Path
                    ) -> tuple[Path | None, dict[Path, Snapshot], dict[Path, Snapshot]]:
    """Stage all changes and recheck every destination before first replacement."""
    targets = [Path(os.path.abspath(path)) for path, _, _ in plan]
    read_set = getattr(plan, "read_set", {})
    initial = {path: read_set[path] if path in read_set else snapshot(path) for path in targets}
    for path in targets:
        if snapshot(path) != initial[path]:
            raise RuntimeError(f"target changed after planning: {path}")
    changes = [(path, content.encode(), mode) for (path, content, mode), path in zip(plan, targets)
               if initial[path] != (True, content.encode(), mode)]
    for path, _, _ in changes:
        check_parent_chain(path)
    backup = backup_changes(changes, initial, home)
    return commit_staged(changes, initial, backup)


def commit_staged(changes: list[tuple[Path, bytes, int]], initial: dict[Path, Snapshot],
                  backup: Path | None
                  ) -> tuple[Path | None, dict[Path, Snapshot], dict[Path, Snapshot]]:
    """Stage in place, recheck for concurrent edits, then atomically replace."""
    staged: list[tuple[Path, Path, int]] = []
    committed: list[Path] = []
    expected_after = {path: (True, raw, mode) for path, raw, mode in changes}
    try:
        stage_all(changes, staged)
        for path, _, _ in staged:
            if snapshot(path) != initial[path]:
                raise RuntimeError(f"target changed during preflight: {path}")
        for path, temporary, _ in staged:
            os.replace(temporary, path)
            committed.append(path)
    except Exception:
        restore_files(initial, committed, expected_after)
        raise
    finally:
        for _, temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    changed_initial = {path: initial[path] for path, _, _ in changes}
    changed_expected = {path: expected_after[path] for path, _, _ in changes}
    return backup, changed_initial, changed_expected


def stage_all(changes: list[tuple[Path, bytes, int]], staged: list[tuple[Path, Path, int]]) -> None:
    """Write fsynced same-directory temporary files without touching targets."""
    for path, raw, mode in changes:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".agent-plugin-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        staged.append((path, Path(temporary), mode))


def ensure_bundle_root(root: Path) -> None:
    """Create or validate the private immutable-bundle directory."""
    if root.is_symlink():
        raise ValueError("agent bundle root cannot be a symlink")
    if root.exists() and (not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise ValueError("agent bundle root has unsafe type or permissions")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)


def validate_current(current: Path, root: Path) -> str | None:
    """Verify the exact managed pointer and every file in its immutable bundle."""
    if current.is_symlink():
        value = os.readlink(current)
        if (Path(value).name != value or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)):
            raise ValueError("agent current pointer is not a bundle name")
        target = root / value
        return verify_bundle(target)
    if current.exists():
        raise ValueError("agent current path is not an owned symlink")
    return None


def verify_bundle(bundle: Path, expected: dict[str, str] | None = None) -> str:
    """Check bundle name, manifest, exact file set, hashes and private modes."""
    if bundle.is_symlink() or not bundle.is_dir() or stat.S_IMODE(bundle.stat().st_mode) != 0o700:
        raise ValueError("agent bundle directory is invalid")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("agent bundle manifest is invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("agent bundle manifest is invalid")
    files = manifest.get("files")
    if (not isinstance(files, dict) or set(files) != RUNTIME_FILES
            or (expected is not None and files != expected)):
        raise ValueError("agent bundle file list is invalid")
    if {item.name for item in bundle.iterdir()} != set(files) | {"manifest.json"}:
        raise ValueError("agent bundle has unexpected files")
    if stat.S_IMODE(manifest_path.stat().st_mode) != 0o600:
        raise ValueError("agent bundle manifest permissions are invalid")
    verify_bundle_files(bundle, files)
    actual = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if manifest.get("bundleHash") != actual or bundle.name != actual:
        raise ValueError("agent bundle content address is invalid")
    return actual


def verify_bundle_files(bundle: Path, files: dict[str, Any]) -> None:
    """Validate exact file type, private mode and digest for every bundle item."""
    for name, digest in files.items():
        path = bundle / name
        if (not isinstance(name, str) or not isinstance(digest, str) or path.is_symlink()
                or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("agent bundle file was changed")


def switch_current(current: Path, root: Path, digest: str, previous: str | None) -> None:
    """Validate then atomically switch current, preserving the prior pointer."""
    ensure_bundle_root(root)
    if verify_bundle(root / digest) != digest or validate_current(current, root) != previous:
        raise RuntimeError("agent bundle/current changed during install")
    temporary = root / f".current-{uuid4().hex}"
    os.symlink(digest, temporary)
    try:
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def restore_current(current: Path, root: Path, previous: str | None, expected: str) -> None:
    """Restore a pointer only while it still targets this transaction's bundle."""
    if not current.is_symlink() or os.readlink(current) != expected:
        raise RuntimeError("agent current changed; refusing rollback")
    if previous:
        temporary = root / f".restore-{uuid4().hex}"
        os.symlink(previous, temporary)
        try:
            os.replace(temporary, current)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        current.unlink()
