"""Exercise late conversation routing through the real prompt and Stop adapters.

Fixtures use one native rollout and real local Git metadata. No test invokes a
model or writes into a shared Wiki; assertions cover both eligible recovery and
the boundaries that must keep unrelated or ambiguous turns out of the queue.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from common import load_json
from routing import remote_identity


class TurnRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "compiler"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/mine/compiler.git"], check=True)
        self.rollout = self.root / "sessions" / "one.jsonl"
        self.rollout.parent.mkdir()
        self.config = {"enabled": True, "intakeEnabled": True, "stateDir": str(self.root / "state"),
                       "env": {"CODEX_HOME": str(self.root)}, "workingForks": ["mine/compiler"],
                       "eventDriven": {"enabled": True, "debounceSeconds": 120},
                       "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放"],
                                                "requiredTerms": ["PC"], "pages": []}}}
        self.event = {"hook_event_name": "UserPromptSubmit", "session_id": "session", "turn_id": "turn",
                      "cwd": str(self.root), "prompt": "帮我检查这个问题",
                      "transcript_path": str(self.rollout)}

    def tearDown(self):
        self.temp.cleanup()

    def row(self, kind, payload):
        return {"type": kind, "payload": payload, "timestamp": "2026-09-16T00:00:00Z"}

    def message(self, role, text, turn="turn", channel=None):
        return self.row("response_item", {"type": "message", "role": role, "channel": channel,
                        "content": [{"type": "text", "text": text}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn}})

    def transcript(self, messages, prior=None):
        rows = [self.row("session_meta", {"session_id": "session", "cwd": self.event["cwd"]})]
        rows.extend(prior or [])
        rows.append(self.row("turn_context", {"turn_id": "turn", "cwd": self.event["cwd"]}))
        rows.extend(messages)
        rows.append(self.row("event_msg", {"type": "task_complete", "turn_id": "turn"}))
        self.rollout.write_text("\n".join(json.dumps({**row, "ordinal": i}) for i, row in enumerate(rows)) + "\n")

    def stop(self, assistant="检查完成"):
        # Native Stop has no prompt; recovery must use validated current messages.
        event = {key: value for key, value in self.event.items() if key != "prompt"}
        event.update(hook_event_name="Stop", last_assistant_message=assistant)
        with patch.object(hooks, "invoke") as invoke:
            hooks.handle(event, self.config)
            invoke.assert_not_called()
        return list((Path(self.config["stateDir"]) / "queue").glob("*.json"))

    def test_project_discovered_in_reply_routes_and_binds_next_turn(self):
        hooks.handle(self.event, self.config)
        self.assertNotIn("prompt", load_json(hooks.event_path(self.config, self.event)))
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", f"已确认源码位于 `{self.repo}`，本轮修复保留独立审核。")])
        jobs = self.stop()
        self.assertEqual(len(jobs), 1)
        job = load_json(jobs[0])
        self.assertEqual((job["projectId"], job["repoIdentity"]), ("repo-mine-compiler", "mine/compiler"))
        self.assertEqual(job["evidence"][1]["evidenceStatus"], "assistant-claim")
        self.assertIn("notBefore", job)
        self.event.update(turn_id="next", prompt="继续")
        hooks.handle(self.event, self.config)
        self.assertEqual(load_json(hooks.event_path(self.config, self.event))["projectId"], "repo-mine-compiler")

    def test_ordinary_workspace_path_does_not_hide_an_identified_repository(self):
        hooks.handle(self.event, self.config)
        source = self.repo / "hooks.py"
        source.write_text("# source")
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", f"任务目录为 `{self.root}`；修改见[代码]({source}:14)中。")])
        self.assertEqual(load_json(self.stop()[0])["projectId"], "repo-mine-compiler")

    def test_business_topic_discovered_during_turn_needs_no_special_save_phrase(self):
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("user", "是 ProductX PC 的投放复盘"),
                         self.message("assistant", "两组 cohort 应按成熟观察窗口比较。")])
        self.assertEqual(load_json(self.stop()[0])["projectId"], "growth")

    def test_reply_can_complete_a_business_domain_from_current_user_context(self):
        self.event["prompt"] = "ProductX PC 的这一组怎么解释"
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", "这组投放的回收变化需要对齐观察窗口后再比较。")])
        self.assertEqual(load_json(self.stop()[0])["projectId"], "growth")

    def test_install_during_active_turn_can_recover_without_prompt_record(self):
        self.transcript([self.message("user", "修复 https://github.com/mine/compiler/pull/3"),
                         self.message("assistant", "采用独立审核并保留原始证据。")])
        self.assertEqual(load_json(self.stop()[0])["repoIdentity"], "mine/compiler")

    def test_old_turn_alone_cannot_route_current_unrelated_discussion(self):
        hooks.handle(self.event, self.config)
        prior = [self.message("user", "ProductX PC 投放", turn="old")]
        self.transcript([self.message("user", "这个词是什么意思"),
                         self.message("assistant", "这里给出普通词义解释。")], prior)
        self.assertEqual(self.stop(), [])
        self.assertFalse((Path(self.config["stateDir"]) / "sessions").exists())

    def test_hidden_assistant_analysis_cannot_supply_a_project(self):
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", "ProductX PC 投放", channel="analysis"),
                         self.message("assistant", "这轮没有发现可确认的项目归属。")])
        self.assertEqual(self.stop(), [])

    def test_different_business_and_repo_do_not_get_combined_into_one_job(self):
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", "ProductX PC 投放；https://github.com/mine/compiler")])
        self.assertEqual(self.stop(), [])

    def test_foreign_repo_in_reply_is_not_collected(self):
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", "源码 https://github.com/outsider/compiler")])
        self.assertEqual(self.stop(), [])

    def test_general_prompt_denial_is_not_overridden_by_reply(self):
        self.event["prompt"] = "今天天气"
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"]),
                         self.message("assistant", "ProductX PC 投放")])
        self.assertEqual(self.stop(), [])

    def test_reader_does_not_capture_an_unbound_turn(self):
        self.config["intakeEnabled"] = False
        with patch("turn_routing.capture_evidence_result") as capture:
            self.assertEqual(self.stop(), [])
            capture.assert_not_called()

    def test_incomplete_transcript_cannot_create_a_route(self):
        hooks.handle(self.event, self.config)
        self.rollout.write_text('{"ordinal":0,"type":"session_meta"}\n{"ordinal":1')
        self.assertEqual(self.stop("ProductX PC 投放复盘"), [])

    def test_internal_worker_descendants_remain_excluded(self):
        self.event["cwd"] = str(self.root / "llmwiki-codex-agent-test" / "nested")
        self.event["prompt"] = "ProductX PC 投放复盘"
        hooks.handle(self.event, self.config)
        self.transcript([self.message("user", self.event["prompt"])])
        with patch("turn_routing.capture_evidence_result") as capture:
            self.assertEqual(self.stop(), [])
            capture.assert_not_called()

    def test_excluded_runtime_path_stays_excluded(self):
        self.config["excludedPaths"] = [str(self.root)]
        self.transcript([self.message("user", "ProductX PC 投放复盘")])
        self.assertEqual(self.stop(), [])

    def test_github_repository_pages_keep_strict_host_and_path_validation(self):
        for suffix in ("pull/3", "pull/3/files", "issues/2", "tree/feat/new", "blob/main/src/main.py", "commit/abcdef123"):
            self.assertEqual(remote_identity("https://github.com/mine/compiler/" + suffix), "mine/compiler")
        for url in ("https://github.com.evil/mine/compiler/pull/3", "https://github.com/mine/compiler/settings",
                    "https://github.com/mine/compiler/pull/nope", "https://github.com/mine/compiler/arbitrary/path"):
            self.assertIsNone(remote_identity(url))


if __name__ == "__main__":
    unittest.main()
