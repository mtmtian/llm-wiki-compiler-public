"""Tests for race-resistant shared Wiki file access."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared_files
from shared_files import SharedFiles


TX = "0123456789abcdef0123456789abcdef"
TX2 = "fedcba9876543210fedcba9876543210"


class SharedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "shared"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_root_and_path_links_are_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        root_link = Path(self.temp.name) / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            with SharedFiles(root_link):
                pass
        (self.root / "docs").symlink_to(outside, target_is_directory=True)
        with SharedFiles(self.root) as files:
            with self.assertRaises(ValueError):
                files.read("docs/a.md")
        (self.root / "leaf.md").symlink_to(outside / "leaf.md")
        with SharedFiles(self.root) as files:
            with self.assertRaises(ValueError):
                files.read("leaf.md")
            with self.assertRaises(ValueError):
                files.update("leaf.md", b"new", None, TX)

    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "pipe")
        with SharedFiles(self.root) as files:
            with self.assertRaises(ValueError):
                files.read("pipe")

    def test_bounded_read_rejects_before_collecting_an_oversized_file(self):
        (self.root / "large.json").write_bytes(b"x" * 32)
        with SharedFiles(self.root) as files:
            with self.assertRaisesRegex(ValueError, "maximum"):
                files.read("large.json", max_bytes=16)

    def test_bounded_read_accepts_exact_limit_and_probes_eof(self):
        expected = b"x" * 16
        (self.root / "exact.json").write_bytes(expected)
        with SharedFiles(self.root) as files:
            self.assertEqual(files.read("exact.json", max_bytes=len(expected)), expected)

    def test_bounded_read_rejects_growth_after_initial_stat(self):
        path = self.root / "growing.json"
        path.write_bytes(b"x" * 16)
        original_read = shared_files.os.read
        grew = False

        def grow_then_read(descriptor, size):
            nonlocal grew
            if not grew:
                with path.open("ab") as stream:
                    stream.write(b"!")
                grew = True
            return original_read(descriptor, size)

        with SharedFiles(self.root) as files, patch.object(shared_files.os, "read", grow_then_read):
            with self.assertRaisesRegex(ValueError, "maximum"):
                files.read("growing.json", max_bytes=16)

    def test_bounded_update_rejects_existing_giant_without_reading(self):
        (self.root / "giant.json").write_bytes(b"x" * 32)
        read_bytes = 0
        original_read = shared_files.os.read

        def track_read(descriptor, size):
            nonlocal read_bytes
            block = original_read(descriptor, size)
            read_bytes += len(block)
            return block

        with SharedFiles(self.root) as files, patch.object(shared_files.os, "read", track_read):
            with self.assertRaisesRegex(ValueError, "maximum"):
                files.update("giant.json", b"new", None, TX, max_bytes=16)
        self.assertEqual(read_bytes, 0)

    def test_create_replace_delete_and_idempotent_retries(self):
        with SharedFiles(self.root) as files:
            self.assertIsNone(files.update("wiki/a.md", b"old", None, TX))
            self.assertEqual(files.read("wiki/a.md"), b"old")
            backup = files.update("wiki/a.md", b"new", b"old", TX)
            self.assertIsNotNone(backup)
            self.assertEqual(files.read("wiki/a.md"), b"new")
            self.assertEqual(files.read(backup), b"old")
            self.assertEqual(files.update("wiki/a.md", b"new", b"old", TX), backup)
            removed = files.update("wiki/a.md", None, b"new", TX2)
            self.assertIsNotNone(removed)
            self.assertIsNone(files.read("wiki/a.md"))
            self.assertEqual(files.update("wiki/a.md", None, b"new", TX2), removed)

    def test_human_edit_before_backup_rename_is_restored(self):
        (self.root / "doc.md").write_bytes(b"expected")
        original = shared_files._rename_noreplace
        injected = False

        def edit_then_rename(parent_fd, source, destination):
            nonlocal injected
            if not injected and destination != "doc.md":
                descriptor = os.open("doc.md", os.O_WRONLY, dir_fd=parent_fd)
                try:
                    os.ftruncate(descriptor, 0)
                    os.write(descriptor, b"human")
                finally:
                    os.close(descriptor)
                injected = True
            return original(parent_fd, source, destination)

        with SharedFiles(self.root) as files, patch.object(shared_files, "_rename_noreplace", edit_then_rename):
            with self.assertRaises(ValueError):
                files.update("doc.md", b"desired", b"expected", TX)
            self.assertEqual(files.read("doc.md"), b"human")
            self.assertIsNone(files.read(shared_files._backup_name(TX, "doc.md")))

    def test_concurrent_create_is_preserved(self):
        original = os.link
        injected = False

        def create_human(source, destination, *args, **kwargs):
            nonlocal injected
            if not injected and destination == "new.md":
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kwargs["dst_dir_fd"])
                os.write(descriptor, b"human")
                os.close(descriptor)
                injected = True
            return original(source, destination, *args, **kwargs)

        with SharedFiles(self.root) as files, patch.object(shared_files.os, "link", create_human):
            with self.assertRaises(ValueError):
                files.update("new.md", b"desired", None, TX)
            self.assertEqual(files.read("new.md"), b"human")

    def test_partial_backup_recovery_finishes_transaction(self):
        (self.root / "recover.md").write_bytes(b"old")
        backup = shared_files._backup_name(TX, "recover.md")
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            shared_files._rename_noreplace(descriptor, "recover.md", backup)
        finally:
            os.close(descriptor)
        with SharedFiles(self.root) as files:
            self.assertEqual(files.update("recover.md", b"new", b"old", TX), backup)
            self.assertEqual(files.read("recover.md"), b"new")
            self.assertEqual(files.read(backup), b"old")

    def test_traversal_and_invalid_transaction_are_rejected(self):
        with SharedFiles(self.root) as files:
            for path in ("../x", "a/../x", "/tmp/x", "a//x", "./x", "a\\x"):
                with self.assertRaises(ValueError):
                    files.read(path)
            with self.assertRaises(ValueError):
                files.update("x", b"x", None, "short")


if __name__ == "__main__":
    unittest.main()
