"""Given/When/Then coverage for the per-project decision digest and its hook delivery."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hooks
from current_decisions import MAX_CHARS, current_decisions


def claim(text, kind="decision", status="decided", subject="投放结构"):
    """Build one publication claim with the fields the digest reads."""
    return {"text": text, "kind": kind, "status": status, "decisionObject": subject}


class DigestFixture(unittest.TestCase):
    """Shared private state; holds no tests so subclasses do not rerun each other's cases."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.visible = []
        self.config = {"stateDir": str(self.state), "projects": {"growth": {"label": "增长"}}}

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, number, created, claims, project="growth", visible=True):
        """Cache one verified packet and optionally mark it fully visible in the active replica."""
        record_id = f"{number:064x}"
        path = self.state / "replica-records" / f"{record_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": record_id, "payload": {
            "projectId": project, "createdAt": created, "claims": claims}}, ensure_ascii=False))
        if visible:
            self.visible.append(record_id)
        status = self.state / "replica" / "status.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({"fullyVisibleRecordIds": self.visible}))


class DecisionDigestTests(DigestFixture):
    def test_digest_keeps_visible_decided_rules_newest_first(self):
        """Given mixed records, When the digest renders, Then only visible decided rules appear, newest first."""
        self.publish(1, "2026-09-20T00:00:00Z", [claim("旧规则"), claim("只看过程", kind="lesson", status="historical"),
                                                claim("素材不得重复", kind="constraint", subject="素材")])
        self.publish(2, "2026-09-24T00:00:00Z", [claim("新规则"), claim("新规则")])
        self.publish(3, "2026-09-25T00:00:00Z", [claim("别的业务")], project="other")
        self.publish(4, "2026-09-26T00:00:00Z", [claim("未发布")], visible=False)
        text, fingerprint = current_decisions(self.config, "growth")
        self.assertIn("【增长】", text)
        self.assertLess(text.index("2026-09-24 决定：新规则"), text.index("2026-09-20 决定：旧规则"))
        self.assertLess(text.index("· 投放结构"), text.index("· 素材"))
        self.assertIn("约束：素材不得重复", text)
        self.assertEqual(text.count("新规则"), 1)
        for excluded in ("只看过程", "别的业务", "未发布"):
            self.assertNotIn(excluded, text)
        self.assertTrue(fingerprint)

    def test_unreadable_inputs_only_omit_themselves(self):
        """Given a corrupt record or status file, When the digest renders, Then it never raises."""
        self.publish(1, "2026-09-20T00:00:00Z", [claim("可读规则")])
        self.publish(2, "2026-09-21T00:00:00Z", [claim("损坏规则")])
        (self.state / "replica-records" / f"{2:064x}.json").write_text("{broken")
        self.assertIn("可读规则", current_decisions(self.config, "growth")[0])
        (self.state / "replica" / "status.json").write_text("{broken")
        self.assertEqual(current_decisions(self.config, "growth"), ("", ""))

    def test_budget_keeps_whole_groups_within_the_character_limit(self):
        """Given more rules than fit, When the digest renders, Then whole groups stop at the budget."""
        for number in range(1, 30):
            self.publish(number, f"2026-09-{number:02d}T00:00:00Z", [claim("规则" * 60, subject=f"对象{number}")])
        text, _ = current_decisions(self.config, "growth")
        self.assertLessEqual(len(text), MAX_CHARS)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("对象29", text)
        self.assertNotIn("对象1\n", text)


class DecisionDeliveryTests(DigestFixture):
    def setUp(self):
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        wiki = self.root / "wiki"
        (wiki / "wiki/concepts").mkdir(parents=True)
        (wiki / "wiki/concepts/p1.md").write_text("---\nprojectId: growth\n---\nDecision\n")
        self.config.update({"enabled": True, "intakeEnabled": False, "wikiRoot": str(wiki),
                            "maxContextChars": 120, "owners": ["mine"], "workingForks": [], "excludedRepos": []})
        self.config["projects"]["growth"].update({"aliases": ["ProductX"], "topicTerms": ["投放"],
                                                  "paths": [str(self.workspace)], "pages": ["concepts/p1"]})
        self.event = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "turn_id": "t1",
                      "cwd": str(self.workspace), "prompt": "ProductX投放复盘"}

    def ask(self, turn):
        """Deliver one routed prompt with a stub worker and return the injected context."""
        worker = {"context": "检索结果", "seen": {}, "status": "ok", "complete": True, "diagnostics": {},
                  "references": []}
        with patch.object(hooks, "invoke", return_value=worker), \
                patch("routing.git_identity", return_value=(None, None)):
            result = hooks.handle({**self.event, "turn_id": turn}, self.config)
        return result["hookSpecificOutput"]["additionalContext"]

    def test_prompt_delivers_digest_once_and_again_after_it_changes(self):
        """Given a routed session, When decisions are unchanged, Then the digest is not repeated."""
        self.publish(1, "2026-09-20T00:00:00Z", [claim("旧规则")])
        self.assertTrue(self.ask("t1").startswith("以下是【增长】"))
        self.assertEqual(self.ask("t2"), "检索结果")
        self.publish(2, "2026-09-24T00:00:00Z", [claim("新规则")])
        self.assertIn("新规则", self.ask("t3"))

    def test_compaction_restates_digest_on_the_next_prompt(self):
        """Given a delivered digest, When the context compacts, Then the next routed prompt restates it."""
        self.publish(1, "2026-09-20T00:00:00Z", [claim("旧规则")])
        self.ask("t1")
        compact = {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1",
                   "cwd": str(self.workspace)}
        with patch.object(hooks, "invoke", return_value={"context": "", "seen": {}, "status": "ok"}):
            hooks.handle(compact, self.config)
        self.assertIn("旧规则", self.ask("t2"))


if __name__ == "__main__":
    unittest.main()
