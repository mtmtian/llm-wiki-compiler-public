"""Bridge contract tests using disposable worker modules and no model calls."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from deployment.agent_plugins import bridge
from deployment.agent_plugins.installer import INSTALL_ROOT, install
from deployment.test_agent_plugins_installer import InstallHome


class CommonDouble:
    """Mirror only the deterministic text/hash helpers used by Pi validation."""

    def __init__(self) -> None:
        self.sanitized = 0

    def digest(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def safe_text(self, value: str, limit: int) -> str:
        self.sanitized += 1
        return value[:limit]


class BridgeTests(unittest.TestCase):
    """Exercise exact registration, dynamic worker selection and bounded evidence."""

    def test_stable_launcher_reads_the_latest_shared_worker_pointer(self) -> None:
        with InstallHome() as home:
            first, second = make_worker(home.path, "first"), make_worker(home.path, "second")
            set_worker(home, first, "first")
            install(home.args("install", "pi"))
            payload = pi_prompt(home)
            launcher = home.path / ".local/bin/llmwiki-agent"
            output = call_launcher(launcher, home, payload)
            self.assertEqual({"worker": "first"}, output)
            set_worker(home, second, "second")
            self.assertEqual({"worker": "second"}, call_launcher(launcher, home, payload))
            payload["profile"] = str(home.path / ".pi/foreign")
            self.assertEqual({}, call_launcher(launcher, home, payload))
            self.assertFalse(list((home.path / INSTALL_ROOT).rglob("__pycache__")))

    def test_pi_evidence_is_verified_before_redaction_and_never_truncated(self) -> None:
        common = CommonDouble()
        payload, evidence = valid_evidence(common)
        accepted = bridge.validate_pi_evidence(evidence, payload, common)
        self.assertEqual(["user", "assistant"], [item["kind"] for item in accepted])
        sanitized = common.sanitized
        huge = "x" * (bridge.MAX_EVIDENCE_CHARS + 1)
        evidence[1].update(text=huge, sha256=common.digest(huge))
        self.assertIsNone(bridge.validate_pi_evidence(evidence, payload, common))
        self.assertEqual(sanitized, common.sanitized)

    def test_pi_evidence_rejects_foreign_or_duplicated_turn_entries(self) -> None:
        common = CommonDouble()
        payload, evidence = valid_evidence(common)
        evidence[1]["locator"] = "pi://other-session/entry/answer"
        self.assertIsNone(bridge.validate_pi_evidence(evidence, payload, common))
        payload, evidence = valid_evidence(common)
        evidence.append(dict(evidence[0], id="second-user", locator="pi://session/entry/second-user"))
        self.assertIsNone(bridge.validate_pi_evidence(evidence, payload, common))


def make_worker(root: Path, name: str) -> Path:
    """Create an isolated config_from/hooks pair that records the chosen worker."""
    worker = root / f"worker-{name}"
    worker.mkdir()
    (worker / "runtime.py").write_text("# selected worker marker\n", encoding="utf-8")
    (worker / "common.py").write_text(
        "import hashlib, json\n"
        "def config_from(path): return json.loads(open(path).read())\n"
        "def digest(value): return hashlib.sha256(value.encode()).hexdigest()\n"
        "def safe_text(value, limit): return value[:limit]\n", encoding="utf-8")
    (worker / "hooks.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "def handle(event, config):\n"
        " p=Path(config['sink']); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        " return {'worker': config['workerLabel']}\n", encoding="utf-8")
    return worker / "runtime.py"


def set_worker(home: InstallHome, worker: Path, label: str) -> None:
    """Update only the central runtime pointer in the isolated master config."""
    config = home.read_config()
    config.update(worker=str(worker), workerLabel=label, sink=str(home.path / "calls.log"))
    home.save_config(config)


def pi_prompt(home: InstallHome) -> dict:
    """Build one visible Pi event bound to its exact installed profile."""
    return {"action": "prompt", "sessionId": "pi-session", "turnId": "turn-a",
            "profile": str((home.path / ".pi/agent").resolve()), "cwd": str(home.path),
            "prompt": "explain the current Wiki rule"}


def call_launcher(launcher: Path, home: InstallHome, payload: dict) -> dict:
    """Run a new bridge process, matching how native hooks invoke the launcher."""
    command = [str(launcher), "--host", "pi", "--config", str(home.config), "--profile",
               str((home.path / ".pi/agent").resolve())]
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def valid_evidence(common: CommonDouble) -> tuple[dict, list[dict]]:
    """Construct a complete native user/assistant pair with original locators."""
    payload = {"sessionId": "session", "turnId": "turn", "prompt": "ask", "promptEntryId": "u1"}
    evidence = []
    for kind, identifier, text in (("user", "u1", "ask"), ("assistant", "a1", "answer")):
        evidence.append({"kind": kind, "id": identifier, "text": text,
                         "locator": f"pi://session/entry/{quote(identifier, safe='')}",
                         "sha256": common.digest(text), "complete": True, "current": True})
    return payload, evidence


if __name__ == "__main__":
    unittest.main()
