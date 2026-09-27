"""Behavioral tests for bounded, read-only operational context."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from operational_context import operational_context
import writer_records


NOW = datetime.now(timezone.utc).isoformat()
COMMIT = "0123456789abcdef0123456789abcdef01234567"


class OperationalContextTests(unittest.TestCase):
    """Expose current host state without treating business questions as operations."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "build-manifest.json").write_text(json.dumps({"commit": COMMIT}), encoding="utf-8")
        self.state = self.root / "state"
        self.state.mkdir()
        self.exchange = self.root / "exchange"
        self.exchange.mkdir()
        self.config = {
            "worker": str(self.runtime / "knowledge-flow" / "worker.mjs"),
            "stateDir": str(self.state),
            "machineId": "peer-a",
            "maxDailyJobs": 100,
            "intakeEnabled": True,
            "publishEnabled": True,
            "exchange": {
                "protocolVersion": 2, "root": str(self.exchange),
                "materializerMachineId": "peer-a", "participants": ["peer-a", "peer-b"],
                "sharedWriter": {"version": 1, "bootstrapMachineId": "peer-b"},
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_budget_does_not_override_the_enforced_daily_limit(self):
        """Given a stale legacy key, When observing budget, Then report the enforced maxDailyJobs."""
        self.config.update({"dailyBudget": 7, "maxDailyJobs": 100})
        result = operational_context(self.config, "Wiki 日预算和运行状态")
        self.assertIn("日预算=100", result)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def valid_writer_chain(self):
        """Create the same sealed bootstrap/request/release shape as production."""
        profile = writer_records.settings(self.config)
        checkpoint = {"files": {}, "retiredBaseline": [],
                      "inputs": {"recordIds": [], "routesHash": None}}
        bootstrap = writer_records.sealed({
            "version": 1, "kind": "bootstrap", "profile": profile,
            "baselineId": "b" * 64, "from": "peer-b", "to": "peer-a",
            "mode": "default", "checkpoint": checkpoint,
        })
        writer_records.write(self.config, writer_records.path_for(bootstrap), bootstrap)
        request = writer_records.sealed({
            "version": 1, "kind": "request", "bootstrapId": bootstrap["id"],
            "from": "peer-b", "requestId": "a" * 64,
        })
        writer_records.write(self.config, writer_records.path_for(request), request)
        release = writer_records.sealed({
            "version": 1, "kind": "release", "bootstrapId": bootstrap["id"],
            "previous": bootstrap["id"], "from": "peer-a", "to": "peer-b",
            "mode": "once", "requestId": request["id"], "checkpoint": checkpoint,
        })
        writer_records.write(self.config, writer_records.path_for(release), release)
        self.write("state/shared-writer-state.json", {
            "profile": profile, "head": release["id"], "adopted": None, "outbox": None,
        })
        return release

    def test_runtime_config_change_is_reflected_without_reading_readme(self):
        self.write("home/.config/llmwiki/README.local.md", "旧版本摘要 owner=peer-b")
        prompt = "请查看 llmwiki worker 当前运行状态"
        first = operational_context(self.config, prompt)
        self.assertIn(COMMIT, first)
        self.assertNotIn("旧版本摘要", first)
        self.config["machineId"] = "peer-b"
        second = operational_context(self.config, prompt)
        self.assertIn("机器=peer-b", second)
        self.assertNotEqual(first, second)

    def test_default_owner_and_actual_owner_are_distinguishable(self):
        self.valid_writer_chain()

        result = operational_context(self.config, "llmwiki 共享写入现在是什么状态")

        self.assertIn("默认=peer-a", result)
        self.assertIn("共享目录可见owner=peer-b", result)
        self.assertIn("模式=once", result)

    def test_old_receipt_mtime_does_not_expire_valid_owner(self):
        self.valid_writer_chain()
        paths = list(self.exchange.glob("v2/shared-writer/**/*.json"))
        for path in paths:
            os.utime(path, (1, 1))

        result = operational_context(self.config, "llmwiki 共享写入状态")

        self.assertIn("共享目录可见owner=peer-b", result)
        self.assertIn("模式=once", result)

    def test_tampered_receipt_is_unknown_and_read_is_not_a_write(self):
        release = self.valid_writer_chain()
        release_path = self.exchange / "v2/shared-writer/releases/peer-a" / f"{release['id']}.json"
        value = json.loads(release_path.read_text())
        value["to"] = "peer-a"
        release_path.write_text(json.dumps(value), encoding="utf-8")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        result = operational_context(self.config, "llmwiki 共享写入状态")

        self.assertIn("共享目录可见owner=未知", result)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_missing_observations_are_unknown_and_business_status_does_not_trigger(self):
        result = operational_context(self.config, "请看游戏发布状态")
        self.assertEqual(result, "")
        result = operational_context(self.config, "请查看 Obsidian 同步状态")
        self.assertIn("未知", result)
        self.assertLessEqual(len(result), 1000)
        self.assertNotIn("实际=peer-a", result)

    def test_old_maintenance_snapshot_is_not_reported_as_current(self):
        self.write("state/maintenance.json", {"at": "2020-01-01T00:00:00+00:00",
                                               "counts": {"review": 99}})
        result = operational_context(self.config, "llmwiki 维护状态")
        self.assertIn("维护观测：历史@2020-01-01T00:00:00+00:00", result)
        self.assertNotIn("review=99", result)


if __name__ == "__main__":
    unittest.main()
