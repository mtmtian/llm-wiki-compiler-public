"""Regression tests for private generation sealing and verification."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from replica_integrity import MANIFEST_PATH, RESPONSE_PATH, read_verified_generation, seal_generation


class ReplicaIntegrityTests(unittest.TestCase):
    """Ensure projection inputs fail closed after a generation is sealed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.generation = Path(self.temp.name) / "generation"
        self.generation.mkdir()
        self.write("sources/evidence.md", "evidence")
        self.write("wiki/concepts/decision.md", "decision")
        self.write("wiki/MOC.md", "moc")
        self.write(RESPONSE_PATH, '{"pages": 1, "conflicts": []}')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative: str, content: str):
        path = self.generation / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def seal(self):
        return seal_generation(self.generation, self.generation.name)

    def test_valid_seal_returns_one_byte_snapshot(self):
        manifest = self.seal()
        snapshot = read_verified_generation(self.generation).projection
        self.assertEqual(snapshot["sources/evidence.md"], b"evidence")
        self.assertEqual(manifest["generationId"], "generation")
        self.assertEqual(len(manifest["files"]), 3)
        self.assertIn(RESPONSE_PATH, {item["path"] for item in manifest["consumerFiles"]})
        response_entry = next(item for item in manifest["consumerFiles"] if item["path"] == RESPONSE_PATH)
        self.assertEqual(manifest["responseSha256"], response_entry["sha256"])

    def test_baseline_consumers_are_sealed_but_projection_stays_markdown_only(self):
        """Given baseline consumers, When sealed, Then all are protected but only promoted files return."""
        for relative, content in {
            "sources/nested/evidence.txt": "text evidence",
            "wiki/reference/data.json": "{\"ok\": true}",
            ".llmwiki/config.json": "{\"version\": 1}",
            ".llmwiki/state.json": "{\"sources\": {}}",
            ".llmwiki/schema.json": "{\"type\": \"object\"}",
        }.items():
            self.write(relative, content)
        manifest = self.seal()
        consumers = {item["path"] for item in manifest["consumerFiles"]}
        self.assertTrue({
            "sources/nested/evidence.txt", "wiki/reference/data.json",
            ".llmwiki/config.json", ".llmwiki/state.json", ".llmwiki/schema.json",
        }.issubset(consumers))
        projection = read_verified_generation(self.generation).projection
        self.assertNotIn("sources/nested/evidence.txt", projection)
        self.assertNotIn("wiki/reference/data.json", projection)
        self.assertNotIn(".llmwiki/config.json", projection)
        for relative in consumers:
            path = self.generation / relative
            original = path.read_bytes()
            path.write_bytes(original + b" tampered")
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                read_verified_generation(self.generation)
            path.write_bytes(original)

    def test_all_generation_consumers_are_sealed(self):
        """Given generated stores, When sealed, Then all regular files are protected."""
        self.write("materialized.bin", "compiled artifact")
        self.write(".llmwiki/embeddings.json", '{"version": 3}')
        self.write(".llmwiki/embeddings.bin", "vectors")
        self.write(".llmwiki/pending-embeddings.json", "[]")
        self.write(".llmwiki/materialization/attempts/job.json", '{"status": "completed"}')
        self.write(".llmwiki/lock", "runtime owner")
        self.write(".llmwiki/.llmwiki-preserved-" + "a" * 32 + "-" + "b" * 16 + ".bak", "old seal")
        manifest = self.seal()
        consumers = {item["path"] for item in manifest["consumerFiles"]}
        self.assertIn("materialized.bin", consumers)
        self.assertIn(".llmwiki/embeddings.bin", consumers)
        self.assertNotIn(".llmwiki/lock", consumers)
        self.assertFalse(any(".llmwiki-preserved-" in path for path in consumers))
        for relative in ("materialized.bin", ".llmwiki/embeddings.json", ".llmwiki/embeddings.bin",
                         ".llmwiki/pending-embeddings.json", ".llmwiki/materialization/attempts/job.json"):
            path = self.generation / relative
            original = path.read_bytes()
            path.write_bytes(original + b" tampered")
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                read_verified_generation(self.generation)
            path.write_bytes(original)

    def test_operational_locks_are_not_integrity_inputs(self):
        """Given a runtime lock, When its owner changes, Then reads remain valid."""
        self.write(".llmwiki/lock", "owner-a")
        self.write(".llmwiki/lock.reclaim", "owner-a")
        self.seal()
        self.write(".llmwiki/lock", "owner-b")
        self.write(".llmwiki/lock.reclaim", "owner-b")
        self.assertEqual(read_verified_generation(self.generation).response, b'{"pages": 1, "conflicts": []}')

    def test_embedding_deletion_and_insertion_fail_closed(self):
        self.write(".llmwiki/embeddings.bin", "vectors")
        self.seal()
        target = self.generation / ".llmwiki/embeddings.bin"
        target.unlink()
        with self.assertRaises(ValueError):
            read_verified_generation(self.generation)
        target.write_bytes(b"vectors")
        self.write(".llmwiki/embeddings.extra", "unexpected")
        with self.assertRaises(ValueError):
            read_verified_generation(self.generation)

    def test_deleted_projection_files_fail_closed(self):
        self.seal()
        original = {relative: (self.generation / relative).read_bytes() for relative in
                    ("sources/evidence.md", "wiki/concepts/decision.md", "wiki/MOC.md")}
        for relative, content in original.items():
            with self.subTest(relative=relative):
                path = self.generation / relative
                path.unlink()
                with self.assertRaises(ValueError):
                    read_verified_generation(self.generation)
                path.write_bytes(content)

    def test_tampered_and_extra_files_fail_closed(self):
        self.seal()
        self.write("sources/evidence.md", "tampered")
        with self.assertRaisesRegex(ValueError, "changed"):
            read_verified_generation(self.generation)
        self.write("sources/evidence.md", "evidence")
        self.write("sources/extra.md", "extra")
        with self.assertRaisesRegex(ValueError, "changed"):
            read_verified_generation(self.generation)

    def test_missing_manifest_and_changed_response_fail_closed(self):
        self.seal()
        (self.generation / MANIFEST_PATH).unlink()
        with self.assertRaises(ValueError):
            read_verified_generation(self.generation)
        self.seal()
        self.write(RESPONSE_PATH, '{"pages": 2, "conflicts": []}')
        with self.assertRaisesRegex(ValueError, "response"):
            read_verified_generation(self.generation)

    def test_reduced_file_set_can_be_sealed_and_verified(self):
        (self.generation / "wiki/MOC.md").unlink()
        (self.generation / "wiki/concepts/decision.md").unlink()
        self.seal()
        self.assertEqual(read_verified_generation(self.generation).projection, {"sources/evidence.md": b"evidence"})

    def test_symlink_and_fifo_are_rejected(self):
        self.seal()
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.generation / "sources/evidence.md").unlink()
        (self.generation / "sources/evidence.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            read_verified_generation(self.generation)
        (self.generation / "sources/evidence.md").unlink()
        os.mkfifo(self.generation / "sources/evidence.md")
        with self.assertRaises(ValueError):
            read_verified_generation(self.generation)

    def test_manifest_generation_binding_and_schema_are_checked(self):
        self.seal()
        path = self.generation / MANIFEST_PATH
        value = json.loads(path.read_text(encoding="utf-8"))
        value["generationId"] = "other"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "generation id"):
            read_verified_generation(self.generation)


if __name__ == "__main__":
    unittest.main()
