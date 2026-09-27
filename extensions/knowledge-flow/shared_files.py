"""Race-resistant access to files in the shared knowledge directory.

``SharedFiles`` keeps an open descriptor for the configured root and resolves
each path component relative to directory descriptors with ``O_NOFOLLOW``.
Updates use immutable temporary files and platform no-replace renames, so an
unexpected editor or concurrent writer is retained and reported as a conflict.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_TRANSACTION = re.compile(r"^[0-9a-fA-F]{32}$")
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 1
try:
    _DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    _FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
except AttributeError as error:  # pragma: no cover - project supports Darwin and Linux
    raise RuntimeError("SharedFiles requires O_DIRECTORY, O_NOFOLLOW, and O_NONBLOCK") from error


def _relative_parts(relative: str) -> tuple[list[str], str]:
    """Validate a portable relative path and split its parent and leaf."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("shared path must be a non-empty relative string")
    if relative.startswith("/") or "\\" in relative:
        raise ValueError("shared path must be relative")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("shared path contains an invalid component")
    return parts[:-1], parts[-1]


def _transaction_id(transaction: str) -> str:
    """Validate and normalize the UUID hex used in backup names."""
    if not isinstance(transaction, str) or not _TRANSACTION.fullmatch(transaction):
        raise ValueError("transaction must be a 32-character UUID hex string")
    return transaction.lower()


def _open_dir(parent_fd: int, name: str) -> int:
    """Open one real child directory without following a link."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("shared path contains a symlink or non-directory") from error
        raise


@contextmanager
def _parent_descriptor(root_fd: int, parts: list[str], create: bool) -> Iterator[int]:
    """Yield a descriptor for a path's parent, optionally creating directories."""
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            try:
                child = _open_dir(descriptor, part)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = _open_dir(descriptor, part)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_entry(parent_fd: int, name: str, max_bytes: int | None = None) -> bytes | None:
    """Read a regular file relative to an open parent descriptor."""
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("shared target is a symlink or non-regular file") from error
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("shared target is not a regular file")
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("maximum bytes must be non-negative")
        size = os.fstat(descriptor).st_size
        if max_bytes is not None and size > max_bytes:
            raise ValueError("shared file exceeds maximum bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk_size = 1024 * 1024
            if max_bytes is not None:
                remaining = max_bytes - total
                chunk_size = 1 if remaining <= 0 else min(chunk_size, remaining + 1)
            block = os.read(descriptor, chunk_size)
            if not block:
                break
            total += len(block)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("shared file exceeds maximum bytes")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _backup_name(transaction: str, relative: str) -> str:
    """Build the deterministic hidden backup basename for one target."""
    short_hash = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
    return f".llmwiki-preserved-{transaction}-{short_hash}.bak"


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    """Atomically rename within one directory while refusing replacement."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        function = library.renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        flags = _RENAME_EXCL
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        flags = _RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "platform lacks an atomic no-replace rename")
    result = function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flags)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def _write_new(parent_fd: int, name: str, content: bytes) -> None:
    """Durably create a file with a no-replace hard-link publication."""
    temporary = f".llmwiki-pending-{hashlib.sha256(os.urandom(16)).hexdigest()[:20]}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    except FileExistsError as error:
        raise ValueError("shared target was concurrently created") from error
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    os.fsync(parent_fd)


def _restore_backup(parent_fd: int, backup: str, target: str) -> None:
    """Restore a moved file without overwriting a concurrent target."""
    try:
        _rename_noreplace(parent_fd, backup, target)
        os.fsync(parent_fd)
    except (FileExistsError, FileNotFoundError) as error:
        raise ValueError("shared target or backup changed during recovery") from error


def _replace_existing(
    parent_fd: int,
    leaf: str,
    content: bytes | None,
    expected: bytes | None,
    backup: str,
    backup_bytes: bytes | None,
    backup_relative: str,
    max_bytes: int | None,
) -> str:
    """Move an expected target aside, verify it, and publish its successor."""
    current = _read_entry(parent_fd, leaf, max_bytes)
    if expected is None or current != expected or backup_bytes is not None:
        raise ValueError("shared target conflicts with expected bytes")
    try:
        _rename_noreplace(parent_fd, leaf, backup)
    except FileNotFoundError as error:
        raise ValueError("shared target changed before backup") from error
    except FileExistsError as error:
        raise ValueError("shared backup already exists") from error
    moved = _read_entry(parent_fd, backup, max_bytes)
    if moved != expected:
        _restore_backup(parent_fd, backup, leaf)
        raise ValueError("shared target changed before backup")
    if content is not None:
        _write_new(parent_fd, leaf, content)
    else:
        os.fsync(parent_fd)
    return backup_relative


def _update_entry(
    parent_fd: int,
    relative: str,
    leaf: str,
    content: bytes | None,
    expected: bytes | None,
    transaction: str,
    max_bytes: int | None,
) -> str | None:
    """Apply one update after its secure parent descriptor is acquired."""
    backup = _backup_name(transaction, relative)
    backup_bytes = _read_entry(parent_fd, backup, max_bytes)
    current = _read_entry(parent_fd, leaf, max_bytes)
    backup_relative = _backup_relative(relative, backup)
    if current == content:
        if backup_bytes is not None and expected is not None and backup_bytes != expected:
            raise ValueError("shared backup conflicts with expected bytes")
        return backup_relative if backup_bytes is not None else None
    if current is None:
        return _recover_missing(parent_fd, leaf, content, expected, backup_bytes, backup_relative)
    return _replace_existing(parent_fd, leaf, content, expected, backup, backup_bytes, backup_relative, max_bytes)


def _recover_missing(
    parent_fd: int,
    leaf: str,
    content: bytes | None,
    expected: bytes | None,
    backup_bytes: bytes | None,
    backup_relative: str,
) -> str | None:
    """Finish a transaction whose backup move completed before a crash."""
    if backup_bytes is not None:
        if expected is None or backup_bytes != expected:
            raise ValueError("shared backup conflicts with expected bytes")
        if content is None:
            os.fsync(parent_fd)
            return backup_relative
    elif expected is not None:
        raise ValueError("shared target is absent")
    if content is None:
        return None
    _write_new(parent_fd, leaf, content)
    return backup_relative if backup_bytes is not None else None


class SharedFiles:
    """Securely read and transactionally update files below one real root."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._root_fd: int | None = None

    def __enter__(self) -> "SharedFiles":
        """Open and pin the real root directory for this context."""
        try:
            metadata = os.lstat(self.root)
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError("shared root must be a real directory")
            descriptor = os.open(self.root, _DIR_FLAGS)
        except (FileNotFoundError, OSError) as error:
            if isinstance(error, OSError) and error.errno not in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise
            raise ValueError("shared root must be a real directory") from error
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("shared root must be a real directory")
        self._root_fd = descriptor
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        """Close the pinned root descriptor."""
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def _require_root(self) -> int:
        """Return the active descriptor or reject use outside a context."""
        if self._root_fd is None:
            raise RuntimeError("SharedFiles must be used as a context manager")
        return self._root_fd

    def read(self, relative: str, max_bytes: int | None = None) -> bytes | None:
        """Read a regular relative file, returning ``None`` when absent."""
        parts, leaf = _relative_parts(relative)
        try:
            with _parent_descriptor(self._require_root(), parts, create=False) as parent_fd:
                return _read_entry(parent_fd, leaf, max_bytes)
        except FileNotFoundError:
            return None

    def update(
        self,
        relative: str,
        content: bytes | None,
        expected: bytes | None,
        transaction: str,
        max_bytes: int | None = None,
    ) -> str | None:
        """Create, replace, or delete one file while retaining a prior version."""
        if content is not None and not isinstance(content, bytes):
            raise TypeError("content must be bytes or None")
        if expected is not None and not isinstance(expected, bytes):
            raise TypeError("expected must be bytes or None")
        if max_bytes is not None and (max_bytes < 0 or content is not None and len(content) > max_bytes):
            raise ValueError("shared file exceeds maximum bytes")
        transaction = _transaction_id(transaction)
        parts, leaf = _relative_parts(relative)
        try:
            with _parent_descriptor(self._require_root(), parts, create=content is not None) as parent_fd:
                return _update_entry(parent_fd, relative, leaf, content, expected, transaction, max_bytes)
        except FileNotFoundError:
            if content is None:
                return None
            raise


def _backup_relative(relative: str, backup: str) -> str:
    """Return a backup path beside its target using POSIX separators."""
    parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
    return f"{parent}/{backup}" if parent else backup
