"""Given/When/Then coverage for scoped read delivery and lifecycle state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from hook_context import prepare_context
from common import load_json
from context_observation import diagnostic_path
from maintenance import context_read_summary


class HookContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        wiki = root / "wiki"
        (wiki / "wiki/concepts").mkdir(parents=True)
        (wiki / "wiki/concepts/p1.md").write_text("---\nprojectId: growth\n---\nDecision\n")
        self.config = {
            "enabled": True, "intakeEnabled": False, "stateDir": str(root / "state"),
            "wikiRoot": str(wiki), "maxContextChars": 120,
            "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放"],
                                      "paths": [str(self.workspace)], "pages": ["concepts/p1"]}},
            "owners": ["mine"], "workingForks": [], "excludedRepos": [],
        }
        self.event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                      "cwd": str(self.workspace), "prompt": "ProductX投放复盘"}

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, *_args):
        return {"context": "当前决定及来源", "seen": {"p1#section": "revision"},
                "status": "ok", "complete": True, "diagnostics": {},
                "references": [{"pageId": "concepts/p1", "pageRevision": "rev-1",
                                "citations": ["^[source.md:12-15]"]}]}

    def test_read_scope_does_not_become_stop_write_scope(self):
        """Given only the read resolver identifies a turn, Then Stop has no write project."""
        self.event["prompt"] = "ProductX 这批用户质量为什么变差了？"
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            result = hooks.handle(self.event, self.config)
        turn = load_json(hooks.event_path(self.config, self.event))
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIsNone(turn.get("projectId"))
        self.assertEqual(turn.get("readProjectId"), "growth")

    def test_unfinished_prompt_retry_receives_current_decision(self):
        """Given an unfinished turn, When evidence changes and prompt retries, Then it receives the update."""
        revised = {**self.worker(), "context": "最新决定及来源"}
        with patch.object(hooks, "invoke", side_effect=[self.worker(), revised]), \
                patch("routing.git_identity", return_value=(None, None)):
            first = hooks.handle(self.event, self.config)
            second = hooks.handle(self.event, self.config)
        self.assertEqual(first["hookSpecificOutput"]["additionalContext"], "当前决定及来源")
        self.assertEqual(second["hookSpecificOutput"]["additionalContext"], "最新决定及来源")

    def test_only_matching_stop_allows_auxiliary_decision_deduplication(self):
        """Given prepared auxiliary evidence, Then only its own Stop permits a later pointer."""
        def cached_worker(_config, _command, payload, _timeout):
            text = "辅助决定指针" if payload["seen"] else "辅助决定全文"
            return {**self.worker(), "context": text}

        with patch.object(hooks, "invoke", side_effect=cached_worker), \
                patch("routing.git_identity", return_value=(None, None)):
            first = hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop", "turn_id": "t2"}, self.config)
            retry = hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop"}, self.config)
            later = hooks.handle({**self.event, "turn_id": "t3"}, self.config)
        self.assertEqual(first["hookSpecificOutput"]["additionalContext"], "辅助决定全文")
        self.assertEqual(retry["hookSpecificOutput"]["additionalContext"], "辅助决定全文")
        self.assertEqual(later["hookSpecificOutput"]["additionalContext"], "辅助决定指针")

    def test_session_start_clear_removes_read_and_write_binding(self):
        session_path = Path(self.config["stateDir"]) / "sessions" / (hooks.digest("s1") + ".json")
        session_path.parent.mkdir(parents=True)
        session_path.write_text('{"projectId":"growth","readProjectId":"growth","seen":{"p":"r"}}')
        event = {"hook_event_name": "SessionStart", "source": "clear", "session_id": "s1",
                 "cwd": str(self.workspace)}
        self.assertEqual(hooks.handle(event, self.config), {})
        self.assertFalse(session_path.exists())

    def test_session_start_compact_reprovides_latest_read_prompt(self):
        """Given a completed read, When context compacts after a decision changes, Then it receives the update."""
        revised = {**self.worker(), "context": "压缩后最新决定及来源"}
        with patch.object(hooks, "invoke", side_effect=[self.worker(), revised]), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop"}, self.config)
            compact = {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1",
                       "cwd": str(self.workspace)}
            result = hooks.handle(compact, self.config)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "压缩后最新决定及来源")
        lifecycle = load_json(diagnostic_path(self.config, compact, "session-compact"))
        self.assertEqual(lifecycle["kind"], "lifecycle")
        self.assertEqual(context_read_summary(Path(self.config["stateDir"]))["preparedEvidenceTurns"], 1)

    def test_startup_does_not_restore_old_read_context(self):
        with patch.object(hooks, "invoke", side_effect=self.worker) as invoke:
            result = hooks.handle({"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1",
                                   "cwd": str(self.workspace)}, self.config)
        self.assertEqual(result, {})
        invoke.assert_not_called()

    def test_session_start_with_turn_id_keeps_lifecycle_separate(self):
        """Given a lifecycle event carries a turn ID, Then it cannot overwrite or add a turn observation."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            original = load_json(diagnostic_path(self.config, self.event, ""))
            for turn_id in (self.event["turn_id"], "another-active-turn"):
                with self.subTest(turn_id=turn_id):
                    compact = {**self.event, "hook_event_name": "SessionStart",
                               "source": "compact", "turn_id": turn_id}
                    hooks.handle(compact, self.config)
                    lifecycle = load_json(diagnostic_path(self.config, compact, "session-compact"))
                    self.assertEqual(lifecycle["kind"], "lifecycle")
                    self.assertIsNone(lifecycle["turnHash"])
                    self.assertEqual(load_json(diagnostic_path(self.config, self.event, "")), original)
                    summary = context_read_summary(Path(self.config["stateDir"]))
                    self.assertEqual(summary["preparedEvidenceTurns"], 1)
                    self.assertEqual(summary["lifecycleRecords"], 1)

    def test_resume_in_third_party_repository_does_not_replay_private_context(self):
        """Given a prior binding, When resumed in an untrusted repo, Then no business context is replayed."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
        event = {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s1",
                 "cwd": str(self.workspace)}
        with patch("routing.git_identity", return_value=(str(self.workspace), "stranger/repo")):
            result = hooks.handle(event, self.config)
        self.assertEqual(result, {})

    def test_unbound_operational_prompt_returns_snapshot_and_diagnostic(self):
        event = {**self.event, "cwd": str(self.root), "prompt": "请查看 llmwiki worker 当前运行状态"}
        with patch.object(hooks, "operational_context", return_value="当前运行提交=abc"), \
                patch.object(hooks, "invoke") as invoke, \
                patch("routing.git_identity", return_value=(None, None)):
            result = hooks.handle(event, self.config)
        self.assertIn("当前运行提交=abc", result["hookSpecificOutput"]["additionalContext"])
        invoke.assert_not_called()
        diagnostic = load_json(diagnostic_path(self.config, event, ""))
        self.assertEqual(diagnostic["status"], "no-scope")

    def test_operational_switch_does_not_replay_bound_business_decisions(self):
        """Given a business conversation, When it switches to Wiki ops, Then old decisions stay out."""
        self.config["maxContextChars"] = 2400
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            business = hooks.handle(self.event, self.config)
            operation = {**self.event, "turn_id": "t2", "cwd": str(self.root),
                         "prompt": "llmwiki worker 当前运行状态"}
            result = hooks.handle(operation, self.config)
            for source in ("compact", "resume"):
                lifecycle = {"hook_event_name": "SessionStart", "source": source,
                             "session_id": "s1", "cwd": str(self.root)}
                self.assertEqual(hooks.handle(lifecycle, self.config), {})
            followup = hooks.handle({**operation, "turn_id": "t3", "prompt": "继续核对当前版本和状态"}, self.config)
        self.assertIn("当前决定及来源", business["hookSpecificOutput"]["additionalContext"])
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Wiki", text)
        self.assertNotIn("当前决定及来源", text)
        self.assertEqual(followup, {})

    def test_context_cap_covers_operational_text_and_does_not_mark_clipped_seen(self):
        calls = []

        def oversized(config, _command, payload, _timeout):
            calls.append((config, payload))
            return {"context": "x" * 500, "seen": {"p": "new"}, "status": "ok", "complete": True,
                    "references": [{"pageId": "concepts/p1", "pageRevision": "rev",
                                    "citations": ["^[source.md:12-15]"]}]}

        result = prepare_context(self.config, "growth", "ProductX投放", ["p1"], {}, oversized,
                                 "当前 runtime 状态")
        self.assertLessEqual(len(result["context"]), 120)
        self.assertEqual(result["seen"], {})
        self.assertFalse(result["prepared"])
        self.assertEqual(result["preparedCount"], 0)
        self.assertEqual(result["references"], [])
        self.assertLess(calls[0][0]["maxContextChars"], self.config["maxContextChars"])
        self.assertNotIn("operationalContext", calls[0][1])

    def test_wrong_turn_stop_does_not_record_a_matching_observation(self):
        """Given turn t1 was prepared, When Stop names t2, Then neither turn gains a Stop observation."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop", "turn_id": "t2",
                          "last_assistant_message": "^[source.md:12-15]"}, self.config)
        record = load_json(diagnostic_path(self.config, self.event, ""))
        self.assertNotIn("stopObservedAt", record)
        self.assertEqual(context_read_summary(Path(self.config["stateDir"]))["matchingStopTurns"], 0)

    def test_redacted_context_does_not_claim_exact_evidence_was_prepared(self):
        """Given local sanitization changes output, Then raw worker references are not counted."""
        def redacted_worker(*args):
            return {**self.worker(*args), "context": "secret=x\n^[source.md:12-15]"}

        result = prepare_context(self.config, "growth", "ProductX投放", ["p1"], {}, redacted_worker)
        self.assertIn("[REDACTED]", result["context"])
        self.assertFalse(result["prepared"])
        self.assertEqual(result["references"], [])
        self.assertEqual(result["seen"], {})

    def test_repeated_matching_stop_counts_once(self):
        """Given one prepared turn, When Stop repeats, Then its match and reference count stay one."""
        stop = {**self.event, "hook_event_name": "Stop",
                "last_assistant_message": "结论 ^[source.md:12-15]"}
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle(stop, self.config)
            hooks.handle(stop, self.config)
        summary = context_read_summary(Path(self.config["stateDir"]))
        self.assertEqual(summary["preparedEvidenceTurns"], 1)
        self.assertEqual(summary["matchingStopTurns"], 1)
        self.assertEqual(summary["explicitReferenceTurns"], 1)

    def test_partial_pack_keeps_rendered_refs_and_stop_clears_temporary_read(self):
        """Given a partial pack has rendered evidence, When Stop cites it, Then it counts and clears temporary state."""
        def partial_worker(*args):
            return {**self.worker(*args), "complete": False}

        with patch.object(hooks, "invoke", side_effect=partial_worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            diagnostic = load_json(diagnostic_path(self.config, self.event, ""))
            hooks.handle({**self.event, "hook_event_name": "Stop",
                          "last_assistant_message": "采用 ^[source.md:12-15]"}, self.config)
        self.assertTrue(diagnostic["preparedEvidence"])
        self.assertEqual(diagnostic["preparedCount"], 1)
        self.assertTrue(load_json(diagnostic_path(self.config, self.event, ""))["explicitReference"])
        session = load_json(hooks.session_path(self.config, "s1"))
        self.assertIsNone(session["preparedRead"])
        self.assertNotIn("p1#section", session.get("seen", {}))

    def test_page_pointers_and_near_page_id_do_not_count_as_source_references(self):
        """Given only pointers or a similarly named page appear, When Stop arrives, Then no reference is counted."""
        def pointer_worker(*args):
            return {**self.worker(*args), "references": []}

        with patch.object(hooks, "invoke", side_effect=pointer_worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop",
                          "last_assistant_message": "补查页 concepts/p1"}, self.config)
        record = load_json(diagnostic_path(self.config, self.event, ""))
        self.assertFalse(record["preparedEvidence"])
        self.assertFalse(record["explicitReference"])

        near = {**self.event, "turn_id": "near"}
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(near, self.config)
            hooks.handle({**near, "hook_event_name": "Stop",
                          "last_assistant_message": "依据 concepts/p1-v2，继续"}, self.config)
        near_record = load_json(diagnostic_path(self.config, near, ""))
        self.assertFalse(near_record["explicitReference"])

        exact = {**self.event, "turn_id": "exact"}
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(exact, self.config)
            hooks.handle({**exact, "hook_event_name": "Stop",
                          "last_assistant_message": "依据 `concepts/p1`，继续"}, self.config)
        exact_record = load_json(diagnostic_path(self.config, exact, ""))
        self.assertTrue(exact_record["explicitReference"])

    def test_unreferenced_final_text_does_not_claim_adoption(self):
        """Given evidence was prepared, When final text omits exact sources, Then adoption remains unverified."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop",
                          "last_assistant_message": "完成，采用当前方案。"}, self.config)
        summary = context_read_summary(Path(self.config["stateDir"]))
        self.assertEqual(summary["explicitReferenceTurns"], 0)
        self.assertEqual(summary["adoption"], "unverified")
        self.assertEqual(summary["delivery"], "unverified")

    def test_unicode_suffix_is_not_a_page_id_boundary(self):
        """Given a page ID is followed by a Chinese word character, Then the partial token is not exact."""
        def source_worker(*args):
            return {**self.worker(*args), "references": [{"pageId": "concepts/foo", "pageRevision": "r",
                                                          "citations": []}]}

        with patch.object(hooks, "invoke", side_effect=source_worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop",
                          "last_assistant_message": "参考 concepts/foo旧版"}, self.config)
        record = load_json(diagnostic_path(self.config, self.event, ""))
        self.assertFalse(record["explicitReference"])

    def test_matching_stop_without_final_text_does_not_become_no_reference(self):
        """Given a matching Stop has no visible final field, Then only Stop observation is counted."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(self.event, self.config)
            hooks.handle({**self.event, "hook_event_name": "Stop"}, self.config)
        summary = context_read_summary(Path(self.config["stateDir"]))
        self.assertEqual(summary["matchingStopTurns"], 1)
        self.assertEqual(summary["finalMessageObservedTurns"], 0)
        self.assertEqual(summary["explicitReferenceTurns"], 0)

    def test_diagnostic_retains_hashes_without_prompt_answer_or_source_text(self):
        """Given distinctive user and final text, When observed, Then the turn diagnostic stores no body."""
        prompt = "ProductX投放复盘 PRIVATE_PROMPT_9081"
        answer = "PRIVATE_ANSWER_7319 ^[source.md:12-15] concepts/p1"
        event = {**self.event, "prompt": prompt}
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            hooks.handle(event, self.config)
            hooks.handle({**event, "hook_event_name": "Stop", "last_assistant_message": answer}, self.config)
        record = load_json(diagnostic_path(self.config, event, ""))
        serialized = json.dumps(record, ensure_ascii=False)
        for private_text in (prompt, "PRIVATE_PROMPT_9081", "PRIVATE_ANSWER_7319",
                             "^[source.md:12-15]", "concepts/p1"):
            self.assertNotIn(private_text, serialized)
        self.assertTrue(record["referenceHashes"])

    def test_truncated_stop_text_only_counts_a_found_reference(self):
        """Given Stop text is clipped, When its prefix matches or not, Then absence stays unknown."""
        with patch.object(hooks, "invoke", side_effect=self.worker), \
                patch("routing.git_identity", return_value=(None, None)):
            found = {**self.event, "turn_id": "clipped-found"}
            hooks.handle(found, self.config)
            hooks.handle({**found, "hook_event_name": "Stop",
                          "last_assistant_message": "^[source.md:12-15]" + "x" * 17000}, self.config)
            unknown = {**self.event, "turn_id": "clipped-unknown"}
            hooks.handle(unknown, self.config)
            hooks.handle({**unknown, "hook_event_name": "Stop",
                          "last_assistant_message": "x" * 17000}, self.config)
        self.assertTrue(load_json(diagnostic_path(self.config, found, ""))["explicitReference"])
        self.assertIsNone(load_json(diagnostic_path(self.config, unknown, ""))["explicitReference"])

    def test_timeout_returns_bounded_project_scoped_degradation(self):
        def timeout(*_args):
            raise TimeoutError("worker timeout")

        result = prepare_context(self.config, "growth", "ProductX投放", ["p1"], {}, timeout,
                                 "当前运行提交=abc")
        self.assertEqual(result["status"], "degraded")
        self.assertIn("growth", result["context"])
        self.assertIn("当前运行提交=abc", result["context"])
        self.assertIn("MCP", result["context"])
        self.assertEqual(result["seen"], {})


if __name__ == "__main__":
    unittest.main()
