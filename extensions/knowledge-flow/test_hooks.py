"""Behavioral isolation tests using temporary repositories and private state."""

import json
import os
import shlex
import tempfile
import time
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

import common
import capture_retry
import hooks
import routing
from common import digest, load_json, save_json


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {"enabled": True, "stateDir": str(self.root), "owners": ["mine", "work"],
                       "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放", "增长"],
                                                "paths": [str(self.root / "growth")], "pages": []},
                                    "app": {"repos": ["work/app"], "pages": []}},
                       "workingForks": ["mine/working-fork"], "excludedRepos": ["mine/research-fork"]}
        self.event = {"session_id": "session", "turn_id": "turn", "cwd": str(self.root),
                      "hook_event_name": "UserPromptSubmit", "prompt": "ProductX投放复盘"}
        save_json(self.root / "repo-metadata.json", {"work/app": {"fork": False, "archived": False, "checkedAt": time.time()}})

    def tearDown(self):
        self.temp.cleanup()

    def test_owned_repo_and_worktree_remote_resolve(self):
        with patch.object(routing, "git_identity", return_value=("/worktree", "work/app")):
            self.assertEqual(routing.resolve("/worktree", "修复接口", None, self.config)[0], "app")

    def test_foreign_repo_cannot_be_overridden_by_business_words(self):
        with patch.object(routing, "git_identity", return_value=("/third-party", "other/app")):
            self.assertIsNone(routing.resolve("/third-party", "ProductX投放", "growth", self.config)[0])

    def test_explicit_working_fork_only(self):
        self.assertTrue(routing.eligible_repo("mine/working-fork", self.config))
        self.assertFalse(routing.eligible_repo("mine/research-fork", self.config))

    def test_unverified_owner_fails_closed(self):
        with patch.object(routing, "run", side_effect=OSError()):
            self.assertFalse(routing.eligible_repo("mine/new", self.config))

    def test_nonrepo_growth_requires_business_identity_and_topic(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertEqual(routing.resolve(str(self.root), "ProductX投放复盘", None, self.config)[0], "growth")
            self.assertIsNone(routing.resolve(str(self.root), "帮我看增长", None, self.config)[0])
            self.assertIsNone(routing.resolve(str(self.root), "ProductX是什么意思", None, self.config)[0])

    def test_growth_path_and_general_question(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertEqual(routing.resolve(str(self.root / "growth"), "分析投放", None, self.config)[0], "growth")
            self.assertIsNone(routing.resolve(str(self.root / "growth"), "今天天气", "growth", self.config)[0])

    def test_continuation_is_bound_but_unrelated_prompt_is_not(self):
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertEqual(routing.resolve(str(self.root), "继续", "growth", self.config)[0], "growth")
            self.assertIsNone(routing.resolve(str(self.root), "解释Python装饰器", "growth", self.config)[0])

    def test_unrelated_event_does_not_store_prompt_or_call_worker(self):
        self.event["prompt"] = "今天天气"
        with patch.object(hooks, "invoke") as worker:
            self.assertEqual(hooks.handle(self.event, self.config), {})
            worker.assert_not_called()
        self.assertNotIn("prompt", load_json(hooks.event_path(self.config, self.event)))

    def test_compiler_recursion_is_excluded(self):
        self.event["cwd"] = str(self.root / "llmwiki-codex-agent-anything")
        with patch.object(hooks, "invoke") as worker:
            self.assertEqual(hooks.handle(self.event, self.config), {})
            worker.assert_not_called()

    def test_duplicate_stop_does_not_reprocess(self):
        save_json(hooks.event_path(self.config, self.event), {"projectId": "growth", "prompt": "选A", "createdAt": "2026-09-14"})
        identifier = hooks.event_path(self.config, self.event).stem
        save_json(self.root / "completed" / (identifier + ".json"), {"status": "empty"})
        self.event["hook_event_name"] = "Stop"
        with patch.object(hooks, "invoke") as worker:
            self.assertEqual(hooks.handle(self.event, self.config), {})
            worker.assert_not_called()

    def test_reader_still_routes_prompts_but_never_collects(self):
        self.config["intakeEnabled"] = False
        self.event["prompt"] = "ProductX投放采用每周复盘"
        with patch.object(routing, "git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
        self.assertEqual(load_json(hooks.event_path(self.config, self.event))["projectId"], "growth")
        self.event["hook_event_name"] = "Stop"
        with patch.object(hooks, "process_queue") as drain:
            self.assertEqual(hooks.handle(self.event, self.config), {})
            drain.assert_not_called()
        self.assertFalse((self.root / "queue").exists())

    def test_reader_does_not_drain_an_existing_queue(self):
        self.config["intakeEnabled"] = False
        save_json(self.root / "queue/j.json", {"id": "j", "projectId": "growth"})
        with patch.object(hooks, "invoke") as worker:
            self.assertEqual(hooks.process_queue(self.config), {"processed": 0, "reason": "intake-disabled"})
            worker.assert_not_called()
        self.assertTrue((self.root / "queue/j.json").exists())
        self.assertFalse((self.root / "daily-budget.json").exists())

    def test_invoke_keeps_launcher_codex_before_node_directory(self):
        """Absolute Node execution does not prepend its directory over launchd's codex path."""
        chosen = self.root / "chosen codex" / "codex"
        chosen.parent.mkdir(parents=True)
        chosen.write_text("#!/bin/sh\nprintf chosen\n", encoding="utf-8")
        chosen.chmod(0o700)
        node = self.root / "node bin" / "node"
        node.parent.mkdir(parents=True)
        shadow = node.parent / "codex"
        shadow.write_text("#!/bin/sh\nprintf shadow\n", encoding="utf-8")
        shadow.chmod(0o700)
        found = self.root / "resolved-codex.txt"
        node.write_text("#!/bin/sh\ncommand -v codex > " + shlex.quote(str(found)) + "\n"
                        "printf '{\"status\":\"empty\"}'\n", encoding="utf-8")
        node.chmod(0o700)
        config = {**self.config, "node": str(node), "worker": str(self.root / "worker.mjs")}
        with patch.dict(os.environ, {"PATH": str(chosen.parent) + ":/usr/bin:/bin"}, clear=False):
            self.assertEqual(hooks.invoke(config, "context", {}, 5)["status"], "empty")
        self.assertEqual(found.read_text().strip(), str(chosen))

    def test_artifact_symlink_escape_is_excluded(self):
        inside = self.root / "task"
        inside.mkdir()
        outside = self.root / "private.md"
        outside.write_text("sensitive outside content")
        (inside / "report.md").symlink_to(outside)
        self.assertEqual(hooks.artifact_evidence(f"[report]({inside}/report.md)", inside), [])

    def test_historical_old_machine_backup_and_readme_artifacts_are_excluded(self):
        paths = [
            self.root / "report.history.md",
            self.root / "old-machine" / "report.md",
            self.root / "backup" / "report.md",
            self.root / "README.md",
            self.root / "README.local.md",
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not become evidence")
        links = " ".join(f"[x]({path})" for path in paths)
        self.assertEqual(hooks.artifact_evidence(links, self.root), [])

    def test_operational_readme_loader_uses_exact_paths_and_tolerates_failure(self):
        local = self.root / ".config/llmwiki/README.local.md"
        shared = self.root / "wiki" / "README.cross-machine.md"
        local.parent.mkdir(parents=True)
        shared.parent.mkdir(parents=True)
        local.write_text("local operational note")
        shared.write_text("shared operational note")
        self.config["wikiRoot"] = str(self.root / "wiki")
        with patch.object(common, "local_operational_readme", return_value=local):
            self.assertEqual(hooks.read_operational_context(self.config),
                             "[README.local.md]\nlocal operational note\n\n[README.cross-machine.md]\nshared operational note")
            shared.unlink()
            self.assertIn("local operational note", hooks.read_operational_context(self.config))

    def test_credential_is_redacted_before_intake(self):
        self.event["last_assistant_message"] = "api_key=sk-abcdefghijklmnopqrstuv"
        record = {"projectId": "growth", "prompt": "采用每周复盘", "createdAt": "2026-09-14"}
        job = hooks.prepare_job(self.event, record, self.config)
        self.assertNotIn("abcdefghijklmnopqrstuv", json.dumps(job))
        self.assertEqual(job["evidence"][0]["sha256"], digest(record["prompt"]))

    def test_host_supplied_complete_conversation_is_preserved(self):
        self.event["conversation_evidence"] = [
            {"id": "alma-user", "kind": "user", "text": "采用每周复盘", "observedAt": "2026-09-14"},
            {"id": "alma-assistant", "kind": "assistant", "text": "已记录", "observedAt": "2026-09-14"},
        ]
        record = {"projectId": "growth", "prompt": "采用每周复盘", "createdAt": "2026-09-14"}
        job = hooks.prepare_job(self.event, record, self.config)
        self.assertEqual([item["kind"] for item in job["evidence"]], ["user", "assistant"])
        self.assertEqual(job["evidence"][1]["sha256"], digest("已记录"))

    def test_replayed_stop_cannot_reset_quarantined_job(self):
        """A repeated native event cannot silently authorize new attempts after quarantine."""
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": "分析已完成"}
        identifier = hooks.event_path(self.config, stop).stem
        failed = self.root / "failed" / (identifier + ".json")
        save_json(failed, {"id": identifier, "attempts": 3})
        with patch.object(hooks, "invoke") as invoke, patch.object(hooks, "prepare_job") as prepare:
            hooks.handle(stop, self.config)
        invoke.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse((self.root / "queue" / (identifier + ".json")).exists())
        self.assertEqual(load_json(failed)["attempts"], 3)

    def test_capture_failure_is_visible_and_retry_can_recover(self):
        self.config["sessionRoots"] = [str(self.root)]
        self.event["transcript_path"] = str(self.root / "missing-rollout.jsonl")
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": "分析已完成"}
        hooks.handle(stop, self.config)
        identifier = hooks.event_path(self.config, stop).stem
        self.assertTrue((self.root / "capture-pending" / (identifier + ".json")).exists())
        self.assertFalse((self.root / "capture-errors" / (identifier + ".json")).exists())
        self.assertFalse((self.root / "completed" / (identifier + ".json")).exists())
        rows = [
            {"ordinal": 0, "type": "session_meta", "payload": {"session_id": "session"}},
            {"ordinal": 1, "type": "turn_context", "payload": {"turn_id": "turn", "cwd": str(self.root)}},
            {"ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": self.event["prompt"]}], "internal_chat_message_metadata_passthrough": {"turn_id": "turn"}}},
            {"ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "分析已完成"}], "internal_chat_message_metadata_passthrough": {"turn_id": "turn"}}},
            {"ordinal": 4, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
        ]
        Path(self.event["transcript_path"]).write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        pending = load_json(self.root / "capture-pending" / (identifier + ".json"))
        pending["nextAttemptAt"] = "2000-01-01T00:00:00+00:00"
        save_json(self.root / "capture-pending" / (identifier + ".json"), pending)
        capture_retry.process_capture_retries(self.config, now=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(len(list((self.root / "queue").glob("*.json"))), 1)
        self.assertFalse((self.root / "capture-pending" / (identifier + ".json")).exists())
        self.assertFalse((self.root / "capture-errors" / (identifier + ".json")).exists())
        self.assertFalse((self.root / "last-error.json").exists())

    def test_duplicate_stop_does_not_replace_pending_snapshot(self):
        """Given pending capture, a later changed Stop cannot replace route or evidence."""
        self.event["transcript_path"] = str(self.root / "missing-rollout.jsonl")
        hooks.handle(self.event, self.config)
        first_stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": "原始分析"}
        hooks.handle(first_stop, self.config)
        identifier = hooks.event_path(self.config, first_stop).stem
        pending_path = self.root / "capture-pending" / (identifier + ".json")
        frozen = load_json(pending_path)
        duplicate = {**first_stop, "prompt": "OtherApp 的无关路由", "last_assistant_message": "改写后的内容",
                     "conversation_evidence": [{"kind": "user", "text": "OtherApp 改写"},
                                                {"kind": "assistant", "text": "新的 artifact"}]}
        hooks.handle(duplicate, self.config)
        self.assertFalse((self.root / "queue" / (identifier + ".json")).exists())
        self.assertEqual(load_json(pending_path), frozen)

    def test_capacity_wait_admission_failure_keeps_full_input_and_duplicate_is_quiet(self):
        self.config["maxQueuedJobs"] = 0
        hooks.handle(self.event, self.config)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": "分析结果有明确变化"}
        hooks.handle(stop, self.config)
        identifier = hooks.event_path(self.config, stop).stem
        error_path = self.root / "capture-errors" / (identifier + ".json")
        self.assertEqual(load_json(error_path)["type"], "CapacityAdmissionError")
        self.assertEqual(load_json(self.root / "failed" / (identifier + ".json"))["status"],
                         "capacity-admission-failed")
        self.config["maxQueuedJobs"] = 1
        stop["conversation_evidence"] = [{"kind": "user", "text": self.event["prompt"]}]
        hooks.handle(stop, self.config)
        self.assertFalse((self.root / "queue" / (identifier + ".json")).exists())
        self.assertTrue(error_path.exists())
        self.assertEqual(load_json(self.root / "last-error.json")["type"], "CapacityAdmissionError")
        self.config["maxQueuedJobs"] = 0
        hooks.handle(stop, self.config)
        self.assertTrue(error_path.exists())

    def test_github_lookalike_is_not_owned(self):
        self.assertIsNone(routing.remote_identity("https://github.com.attacker.test/work/app"))
        self.assertEqual(routing.remote_identity("git@github.com:work/app.git"), "work/app")

    def test_bound_growth_followup_and_other_project(self):
        self.config["projects"]["app"]["aliases"] = ["OtherApp"]
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertEqual(routing.resolve(str(self.root), "看上周投放", "growth", self.config)[0], "growth")
            self.assertIsNone(routing.resolve(str(self.root), "OtherApp投放", "growth", self.config)[0])

    def test_retry_quarantines_without_blocking_other_jobs(self):
        job = {"id": "j", "projectId": "growth", "attempts": 2}
        save_json(self.root / "queue/j.json", job)
        with patch.object(hooks, "invoke", return_value={"status": "error"}):
            hooks.process_queue(self.config)
        self.assertFalse((self.root / "queue/j.json").exists())
        self.assertEqual(load_json(self.root / "failed/j.json")["attempts"], 3)

    def test_prepare_failure_does_not_consume_daily_budget(self):
        """Given replica preparation fails, Then no model budget is consumed."""
        save_json(self.root / "queue/j.json", {"id": "j", "projectId": "growth"})
        with patch("queue_worker.replica_enabled", return_value=True), \
                patch("queue_worker.prepare_replica", side_effect=ValueError("basis overflow")):
            hooks.process_queue(self.config)
        self.assertFalse((self.root / "daily-budget.json").exists())

    def test_queue_scope_is_refreshed_before_processing(self):
        self.config["wikiRoot"] = str(self.root)
        folder = self.root / "wiki/concepts"
        folder.mkdir(parents=True)
        (folder / "new-page.md").write_text("---\nprojectId: growth\n---\nAccepted new page")
        save_json(self.root / "pages.json", {"growth": ["concepts/new-page"]})
        save_json(self.root / "queue/j.json", {"id": "j", "projectId": "growth", "allowedPageIds": []})
        with patch.object(hooks, "invoke", return_value={"status": "empty"}) as worker:
            hooks.process_queue(self.config)
            self.assertEqual(worker.call_args.args[2]["job"]["allowedPageIds"], ["concepts/new-page"])

    def test_synced_pages_retain_project_scope(self):
        from common import page_ids
        self.config["wikiRoot"] = str(self.root)
        folder = self.root / "wiki/concepts"
        folder.mkdir(parents=True)
        name = "growth-" + digest("growth")[:8] + "-decision"
        (folder / (name + ".md")).write_text("accepted")
        (folder / "other-decision.md").write_text("foreign")
        self.assertEqual(page_ids(self.config, "growth"), ["concepts/" + name])

    def test_platform_switch_does_not_inject_the_previous_platform(self):
        self.config["projects"]["growth"]["requiredTerms"] = ["PC"]
        self.config["projects"]["mobile"] = {"aliases": ["ProductX"], "topicTerms": ["投放"], "requiredTerms": ["移动端"]}
        with patch.object(routing, "git_identity", return_value=(None, None)):
            self.assertIsNone(routing.resolve(str(self.root), "ProductX投放", None, self.config)[0])
            self.assertEqual(routing.resolve(str(self.root), "改看移动端投放", "growth", self.config)[0], "mobile")


if __name__ == "__main__":
    unittest.main()
