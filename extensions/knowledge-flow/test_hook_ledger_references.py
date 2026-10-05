"""Given/When/Then coverage for unified hook retrieval and ledger references."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from common import load_json, save_json
from context_observation import diagnostic_path
from maintenance import context_read_summary


class LedgerReferenceHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.wiki = self.root / "wiki"
        (self.wiki / "wiki/concepts").mkdir(parents=True)
        self.config = {"enabled": True, "intakeEnabled": False, "stateDir": str(self.root / "state"),
                       "wikiRoot": str(self.wiki), "maxContextChars": 2400,
                       "projects": {"growth": {"aliases": ["ProductX"], "topicTerms": ["投放"],
                                                 "paths": [str(self.workspace)], "pages": ["concepts/p1"]}},
                       "owners": ["mine"], "workingForks": [], "excludedRepos": []}
        self.event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                      "cwd": str(self.workspace), "prompt": "ProductX投放复盘"}

    def tearDown(self):
        self.temp.cleanup()

    def worker(self, result, calls=None):
        def invoke(_config, command, payload, _timeout):
            if calls is not None:
                calls.append((command, payload))
            return result
        return invoke

    def call_hook(self, event, invoke):
        with patch.object(hooks, "invoke", side_effect=invoke), \
                patch("routing.git_identity", return_value=(None, None)):
            return hooks.handle(event, self.config)

    def test_confirmed_project_with_no_page_allowlist_still_queries_context(self):
        """Given routing confirms a project with no mapped pages, When context is prepared, Then project retrieval runs."""
        self.config["projects"]["growth"]["pages"] = []
        self.event["turn_id"] = "no-pages"
        calls = []
        worker = self.worker({"context": "按任务检索到的约束", "seen": {}, "status": "ok",
                              "complete": True, "diagnostics": {}, "references": []}, calls)

        result = self.call_hook(self.event, worker)

        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "按任务检索到的约束")
        self.assertEqual(calls[0][1]["projectId"], "growth")
        self.assertEqual(calls[0][1]["allowedPageIds"], [])

    def test_claim_reference_is_counted_and_stop_matches_exact_claim_ref(self):
        """Given a ledger claim reference, When Stop repeats that exact reference, Then it is tracked once."""
        claim_ref = f"{'a' * 64}:0"
        worker = self.worker({"context": f"当前记录 {claim_ref}", "seen": {}, "status": "ok",
                              "complete": True, "diagnostics": {}, "references": [{"claimRef": claim_ref,
                                  "recordRevision": "b" * 64, "citations": []}]})
        self.call_hook(self.event, worker)
        stop = {**self.event, "hook_event_name": "Stop", "last_assistant_message": f"依据 `{claim_ref}`"}

        self.call_hook(stop, worker)

        record = load_json(diagnostic_path(self.config, self.event, ""))
        summary = context_read_summary(Path(self.config["stateDir"]))
        self.assertEqual(record["preparedCount"], 1)
        self.assertTrue(record["referencesTracked"])
        self.assertEqual(len(record["referenceHashes"]), 1)
        self.assertTrue(record["explicitReference"])
        self.assertEqual(summary["adoption"], "unverified")
        self.assertEqual(summary["delivery"], "unverified")
        self.assertNotIn(claim_ref, json.dumps(record))

    def test_claim_marker_is_counted_and_stop_matches_exact_token(self):
        """Given context's bracketed ledger marker, When Stop repeats it, Then only the exact claim is observed."""
        claim_ref = f"{'a' * 64}:0"
        marker = f"[claim:{claim_ref}]"
        worker = self.worker({"context": f"当前记录 {marker}", "seen": {}, "status": "ok",
                              "complete": True, "diagnostics": {}, "references": [{"claimRef": claim_ref,
                                  "recordRevision": "b" * 64, "citations": [marker]}]})
        self.call_hook(self.event, worker)

        self.call_hook({**self.event, "hook_event_name": "Stop", "last_assistant_message": f"依据 {marker}"}, worker)

        record = load_json(diagnostic_path(self.config, self.event, ""))
        summary = context_read_summary(Path(self.config["stateDir"]))
        self.assertEqual(record["preparedCount"], 1)
        self.assertTrue(record["explicitReference"])
        self.assertEqual(summary["adoption"], "unverified")

    def test_stop_rejects_approximate_or_glued_claim_markers(self):
        """Given nearby or attached marker text, When Stop arrives, Then the claim is not counted as referenced."""
        claim_ref = f"{'c' * 64}:0"
        record_id = "c" * 64
        worker = self.worker({"context": "检索到一条引用", "seen": {}, "status": "ok", "complete": True,
                              "diagnostics": {}, "references": [{"claimRef": claim_ref,
                                  "recordRevision": "d" * 64, "citations": []}]})
        messages = [f"[claim:{record_id}:1]", f"x[claim:{claim_ref}]", f"[claim:{claim_ref}]9"]

        for index, message in enumerate(messages):
            event = {**self.event, "turn_id": f"near-marker-{index}"}
            self.call_hook(event, worker)
            self.call_hook({**event, "hook_event_name": "Stop", "last_assistant_message": message}, worker)
            self.assertFalse(load_json(diagnostic_path(self.config, event, ""))["explicitReference"])

    def test_stop_requires_exact_claim_reference_or_citation(self):
        """Given a nearby claim-reference token, When Stop arrives, Then partial identity does not count."""
        claim_ref = f"{'c' * 64}:0"
        worker = self.worker({"context": "检索到一条引用", "seen": {}, "status": "ok", "complete": True,
                              "diagnostics": {}, "references": [{"claimRef": claim_ref,
                                  "recordRevision": "d" * 64, "citations": ["^[source.md:7]"]}]})
        near = {**self.event, "turn_id": "near"}
        self.call_hook(near, worker)

        self.call_hook({**near, "hook_event_name": "Stop", "last_assistant_message": claim_ref + "9"}, worker)

        self.assertFalse(load_json(diagnostic_path(self.config, near, ""))["explicitReference"])
        near_citation = {**self.event, "turn_id": "near-citation"}
        self.call_hook(near_citation, worker)
        self.call_hook({**near_citation, "hook_event_name": "Stop",
                        "last_assistant_message": "引用 ^[source.md:70]"}, worker)
        self.assertFalse(load_json(diagnostic_path(self.config, near_citation, ""))["explicitReference"])

    def test_unrelated_project_digest_is_not_concatenated_with_task_context(self):
        """Given an unrelated project-level digest exists, When retrieval runs, Then only its unified task result is injected."""
        record_id = "e" * 64
        records = Path(self.config["stateDir"]) / "replica-records"
        records.mkdir(parents=True)
        save_json(Path(self.config["stateDir"]) / "replica/status.json", {"fullyVisibleRecordIds": [record_id]})
        save_json(records / f"{record_id}.json", {"payload": {"projectId": "growth", "createdAt": "2026-10-05",
            "claims": [{"status": "decided", "kind": "decision", "decisionObject": "另一个主题",
                        "text": "不相关项目级摘要不得注入"}]}})
        worker = self.worker({"context": "唯一的按任务相关性检索结果", "seen": {}, "status": "ok",
                              "complete": True, "diagnostics": {}, "references": []})

        result = self.call_hook(self.event, worker)

        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "唯一的按任务相关性检索结果")

    def test_compact_and_resume_retrieve_again_through_the_unified_worker(self):
        """Given a completed turn, When compact and resume restore context, Then both return fresh unified retrieval."""
        results = ["首轮统一结果", "压缩后统一结果", "恢复后统一结果"]
        calls = []

        def worker(_config, command, payload, _timeout):
            calls.append((command, payload))
            return {"context": results[len(calls) - 1], "seen": {}, "status": "ok", "complete": True,
                    "diagnostics": {}, "references": []}

        first = self.call_hook(self.event, worker)
        self.call_hook({**self.event, "hook_event_name": "Stop"}, worker)
        session_file = hooks.session_path(self.config, "s1")
        session = load_json(session_file)
        save_json(session_file, {**session, "decisionsSeen": {"growth": "old-digest"}})
        compact = self.call_hook({"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1",
                                  "cwd": str(self.workspace)}, worker)
        resume = self.call_hook({"hook_event_name": "SessionStart", "source": "resume", "session_id": "s1",
                                 "cwd": str(self.workspace)}, worker)

        self.assertEqual(first["hookSpecificOutput"]["additionalContext"], "首轮统一结果")
        self.assertEqual(compact["hookSpecificOutput"]["additionalContext"], "压缩后统一结果")
        self.assertEqual(resume["hookSpecificOutput"]["additionalContext"], "恢复后统一结果")
        self.assertTrue(all(command == "context" for command, _ in calls))
        self.assertNotIn("decisionsSeen", load_json(session_file))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
