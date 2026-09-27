"""Native Claude transcript-boundary regression tests without live capture."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.agent_plugins.claude_capture import IncompleteTranscript, evidence


class ClaudeCaptureTests(unittest.TestCase):
    """Verify complete ancestry, final endings and text-only evidence."""

    def test_complete_branch_keeps_native_locators_and_visible_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            result = capture(event, profile)
            self.assertEqual(["user", "assistant"], [item["kind"] for item in result])
            self.assertEqual("ask", result[0]["text"])
            self.assertEqual("answer", result[1]["text"])
            self.assertIn("/message/u1", result[0]["locator"])

    def test_nonfinal_stop_reason_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp), stop_reason="max_tokens")
            with self.assertRaisesRegex(IncompleteTranscript, "completion-not-final"):
                capture(event, profile)

    def test_image_or_foreign_prompt_is_not_claimed_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp), image=True)
            with self.assertRaisesRegex(ValueError, "image-evidence-incomplete"):
                capture(event, profile)
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp), prompt_id="different-prompt")
            with self.assertRaisesRegex(ValueError, "different-prompt"):
                capture(event, profile)

    def test_same_repository_subdirectories_preserve_the_complete_turn(self) -> None:
        """Given one prompt, moving between repository subdirectories keeps its evidence."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"])
            (root / ".git").mkdir()
            first, last = root / "src", root / "tests"
            first.mkdir()
            last.mkdir()
            rewrite_directories(event, [first, last])
            event["cwd"] = str(last)
            self.assertEqual(["ask", "answer"], [item["text"] for item in capture(event, profile)])

    def test_repository_sibling_nested_repo_and_symlink_escape_stay_rejected(self) -> None:
        """A common parent or path spelling must not authorize another repository."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"])
            (root / ".git").mkdir()
            foreign, nested = root.parent / "foreign", root / "nested"
            foreign.mkdir()
            nested.mkdir()
            (nested / ".git").write_text("gitdir: /not-used")
            link = root / "linked"
            link.symlink_to(foreign, target_is_directory=True)
            for destination in [foreign, nested, link]:
                with self.subTest(destination=destination.name):
                    rewrite_directories(event, [root, destination])
                    with self.assertRaisesRegex(ValueError, "foreign-workspace"):
                        capture(event, profile)

    def test_configured_project_directory_preserves_subdirectory_moves(self) -> None:
        """Given a non-repository project directory, moving into its subfolders keeps the turn."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"]).resolve()
            child = root / "scripts"
            child.mkdir()
            rewrite_directories(event, [root, child])
            event["cwd"] = str(child)
            with self.assertRaisesRegex(ValueError, "foreign-workspace"):
                capture(event, profile)
            self.assertEqual(["ask", "answer"], [item["text"] for item in capture(event, profile, {root})])

    def test_configured_project_directory_keeps_outside_and_nested_repositories_separate(self) -> None:
        """A project boundary never admits its parent, a sibling folder or a nested repository."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"]).resolve()
            sibling, nested = root.parent / "sibling", root / "nested"
            sibling.mkdir()
            nested.mkdir()
            (nested / ".git").mkdir()
            for destination in [root.parent, sibling, nested]:
                with self.subTest(destination=destination.name):
                    rewrite_directories(event, [root, destination])
                    with self.assertRaisesRegex(ValueError, "foreign-workspace"):
                        capture(event, profile, {root})

    def test_same_repository_still_rejects_foreign_session_and_sidechain(self) -> None:
        """Directory compatibility never substitutes for native branch identity."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            path = Path(event["transcript_path"])
            original = path.read_text()
            for mutation in [{"sessionId": "foreign"}, {"isSidechain": True}]:
                rows = [json.loads(line) for line in original.splitlines()]
                rows[-1].update(mutation)
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                with self.assertRaisesRegex(ValueError, "foreign-session"):
                    capture(event, profile)

    def test_native_stop_enqueues_subdirectory_evidence_once_through_shared_hooks(self) -> None:
        """A real bridge subprocess must persist one complete job without a model call."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"])
            (root / ".git").mkdir()
            child = root / "src"
            child.mkdir()
            rewrite_directories(event, [root, child])
            event.update(hook_event_name="Stop", cwd=str(child))
            config, queued = bridge_config(event, profile)
            for _ in range(2):
                self.assertEqual({}, run_bridge(event, config, profile))
            job = json.loads(queued.read_text())
            self.assertEqual("ok", job["captureStatus"])
            self.assertEqual(["ask", "answer"], [item["text"] for item in job["evidence"]])
            self.assertEqual([queued], list(queued.parent.glob("*.json")))

    def test_native_stop_uses_configured_project_directories(self) -> None:
        """The adapter passes configured project paths, so a routed subfolder turn is queued."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            root = Path(event["cwd"])
            child = root / "scripts"
            child.mkdir()
            rewrite_directories(event, [root, child])
            event.update(hook_event_name="Stop", cwd=str(child))
            config, queued = bridge_config(event, profile)
            settings = json.loads(config.read_text())
            settings["projects"]["project"]["paths"] = [str(root)]
            config.write_text(json.dumps(settings))
            self.assertEqual({}, run_bridge(event, config, profile))
            job = json.loads(queued.read_text())
            self.assertEqual(["ask", "answer"], [item["text"] for item in job["evidence"]])

    def test_unrouted_turn_leaving_its_folder_is_filtered_without_capture_error(self) -> None:
        """Given an unbound turn that moves to a sibling folder, Stop filters it like shared intake."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = folder_changing_stop(Path(temp))
            config, queued = bridge_config(event, profile, project=None)
            self.assertEqual({}, run_bridge(event, config, profile))
            state = queued.parent.parent
            self.assertEqual([], list((state / "capture-errors").glob("*.json")))
            self.assertFalse((state / "last-error.json").exists())
            status = json.loads((state / "agent-events" / queued.name).read_text())
            self.assertEqual("filtered", status["status"])

    def test_routed_turn_leaving_its_folder_keeps_a_reasoned_capture_error(self) -> None:
        """Given a routed turn whose evidence is rejected, its loss stays visible with the cause."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = folder_changing_stop(Path(temp))
            config, queued = bridge_config(event, profile)
            self.assertEqual({}, run_bridge(event, config, profile))
            error = json.loads((queued.parent.parent / "capture-errors" / queued.name).read_text())
            self.assertEqual(("ClaudeEvidenceUnavailable", "foreign-workspace"), (error["type"], error["reason"]))
            self.assertFalse(queued.exists())


def folder_changing_stop(root: Path) -> tuple[dict, Path]:
    """Build a Stop whose turn starts in a non-repository folder and ends in a sibling."""
    event, profile = fixture(root)
    start = Path(event["cwd"])
    sibling = start.parent / "sibling"
    sibling.mkdir()
    rewrite_directories(event, [start, sibling])
    event.update(hook_event_name="Stop", cwd=str(sibling))
    return event, profile


def run_bridge(event: dict, config: Path, profile: Path) -> dict:
    """Deliver one native hook event through the real bridge subprocess."""
    command = [sys.executable, "-B", str(Path(__file__).parent / "agent_plugins/bridge.py"),
               "--host", "claude", "--config", str(config), "--profile", str(profile)]
    result = subprocess.run(command, input=json.dumps(event), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def rewrite_directories(event: dict, directories: list[Path]) -> None:
    """Move native row directories without changing prompt/session/parent identity."""
    path = Path(event["transcript_path"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row, directory in zip(rows, directories, strict=True):
        row["cwd"] = str(directory)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def bridge_config(event: dict, profile: Path, project: str | None = "project") -> tuple[Path, Path]:
    """Provide real queue storage with the native prompt checkpoint, bound or unrouted."""
    root = profile.parent
    state, config = root / "state", root / "config.json"
    runtime = Path(__file__).resolve().parents[1] / "extensions/knowledge-flow"
    session = "claude:" + hashlib.sha256(str(profile.resolve()).encode()).hexdigest()[:16] + ":session-a"
    identifier = hashlib.sha256((session + ":claude:prompt-a").encode()).hexdigest()
    registration = {"host": "claude", "config": str(config.resolve()), "profile": str(profile.resolve())}
    config.write_text(json.dumps({"version": 1, "enabled": True, "eventDriven": {"enabled": True},
        "wikiRoot": str(root / "wiki"), "stateDir": str(state), "worker": str(runtime / "worker.mjs"),
        "node": str(root / "must-not-launch-model"), "projects": {"project": {"pages": []}},
        "agentPlugins": {"version": 1, "registrations": [registration]}}))
    turn = state / "turns" / (identifier + ".json")
    turn.parent.mkdir(parents=True)
    route = ({"projectId": project, "prompt": "ask", "repoIdentity": "owner/project"} if project
             else {"projectId": None, "reason": "unbound-workspace"})
    turn.write_text(json.dumps({**route, "cwd": event["cwd"], "createdAt": "2026-09-26T00:00:00Z"}))
    return config, state / "queue" / turn.name


def fixture(root: Path, stop_reason: str = "end_turn", image: bool = False,
            prompt_id: str = "prompt-a") -> tuple[dict, Path]:
    """Write one native JSONL turn under the chosen Claude profile."""
    profile, cwd = root / ".claude", root / "workspace"
    transcript = profile / "projects/project/session.jsonl"
    transcript.parent.mkdir(parents=True)
    cwd.mkdir()
    content = [{"type": "text", "text": "ask"}]
    if image:
        content.append({"type": "image", "source": {"type": "base64", "data": "hidden"}})
    rows = [native_row("user", "u1", None, prompt_id, cwd, {"role": "user", "content": content}),
            native_row("assistant", "a1", "u1", None, cwd,
                       {"role": "assistant", "content": [{"type": "text", "text": "answer"}],
                        "stop_reason": stop_reason})]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    event = {"transcript_path": str(transcript), "cwd": str(cwd), "session_id": "session-a",
             "prompt_id": "prompt-a", "last_assistant_message": "answer"}
    return event, profile


def native_row(kind: str, identifier: str, parent: str | None, prompt: str | None,
               cwd: Path, message: dict) -> dict:
    """Build the native session fields required for ancestry validation."""
    return {"type": kind, "uuid": identifier, "parentUuid": parent, "promptId": prompt,
            "sessionId": "session-a", "cwd": str(cwd), "timestamp": "2026-09-23T12:00:00Z",
            "message": message}


def capture(event: dict, profile: Path, project_roots: set[Path] | None = None) -> list[dict]:
    """Provide the shared hash/redaction contract to the native capture reader."""
    common = types.ModuleType("common")
    common.digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    common.safe_text = lambda value, limit: value[:limit]
    with patch.dict(sys.modules, {"common": common}):
        return evidence(event, profile, frozenset(project_roots or ()))


if __name__ == "__main__":
    unittest.main()
