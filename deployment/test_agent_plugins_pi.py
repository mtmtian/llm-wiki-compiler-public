"""Pi 0.87 lifecycle harness for first-turn, repeated-prompt and failure bounds."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

EXTENSION = Path(__file__).parent / "agent_plugins/pi-extension.ts"
HARNESS = r'''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
process.env.LLMWIKI_CONFIG = "/tmp/isolated-knowledge-flow.json";
process.env.LLMWIKI_AGENT_LAUNCHER = "/tmp/isolated-llmwiki-agent";
process.env.PI_CODING_AGENT_DIR = "/tmp/isolated-pi-profile";
const { default: extension } = await import(pathToFileURL(process.argv[2]));
const handlers = new Map();
const calls = [];
const pi = {
  on: (name, handler) => handlers.set(name, handler),
  exec: async (_command, args) => {
    const file = args[args.indexOf("--input-file") + 1];
    const payload = JSON.parse(await readFile(file, "utf8"));
    calls.push(payload);
    const context = payload.action === "prompt" ? {hookSpecificOutput:{additionalContext:"Wiki ref"}} : {};
    return {code:0, killed:false, stdout:JSON.stringify(context)};
  },
};
extension(pi);
const message = (id, role, content, stopReason="stop") => ({type:"message", id,
  message:{role, content, stopReason, timestamp:1700000000000}});
async function lifecycle(id, prompt, initial=[], outcome="completed", end=true, dropLeaf=false) {
  const branch = [...initial];
  const header = {id, cwd:"/workspace/project"};
  const ctx = {cwd:header.cwd, sessionManager:{getHeader:()=>header, getBranch:()=>branch}};
  const response = await handlers.get("before_agent_start")({prompt, images:[]}, ctx);
  if (response?.message) branch.push({type:"custom_message", id:`wiki-${id}`, content:"Wiki ref"});
  if (dropLeaf) branch.length = 0;
  if (end) {
    branch.push(message(`user-${id}`, "user", prompt));
    branch.push(message(`tool-${id}`, "assistant", [{type:"toolCall", id:"tool", name:"read", arguments:{}}], "toolUse"));
    branch.push(message(`think-${id}`, "assistant", [{type:"thinking", thinking:"private reasoning"}], "toolUse"));
    branch.push(message(`answer-${id}`, "assistant", `answer ${id}`));
  }
  await handlers.get("agent_before_settle")({outcome}, ctx);
  await handlers.get("agent_settled")({}, ctx);
  await handlers.get("agent_settled")({}, ctx);
  return response;
}
const firstContext = await lifecycle("empty", "ask");
await lifecycle("repeat", "repeat", [message("old-user", "user", "repeat"), message("old-answer", "assistant", "old")]);
await lifecycle("aborted", "ask", [], "aborted");
await lifecycle("missing-leaf", "again", [message("old-leaf", "assistant", "old")], "completed", true, true);
const tooLarge = "z".repeat(120001);
const largeBranch = [];
const largeCtx = {cwd:"/workspace/project", sessionManager:{getHeader:()=>({id:"large", cwd:"/workspace/project"}), getBranch:()=>largeBranch}};
await handlers.get("before_agent_start")({prompt:"ask", images:[]}, largeCtx);
largeBranch.push(message("user-large", "user", "ask"), message("answer-large", "assistant", tooLarge));
await handlers.get("agent_before_settle")({outcome:"completed"}, largeCtx);
await handlers.get("agent_settled")({}, largeCtx);
const stops = calls.filter((item)=>item.action === "stop");
assert.equal(stops.length, 2, "first turn and repeated old prompt settle once each");
assert.equal(firstContext.message.customType, "llmwiki-context");
assert.match(firstContext.message.content, /Wiki ref/);
assert.equal(firstContext.message.display, false);
for (const stop of stops) {
  assert.deepEqual(stop.evidence.map((item)=>item.id), [`user-${stop.sessionId.includes("repeat") ? "repeat" : "empty"}`,
    `answer-${stop.sessionId.includes("repeat") ? "repeat" : "empty"}`]);
  assert.equal(stop.evidence[0].kind, "user");
  assert.ok(stop.evidence.every((item)=>!item.id.startsWith("wiki-") && !item.id.startsWith("tool-") && !item.id.startsWith("think-")));
}
assert.equal(calls.filter((item)=>item.action === "stop")[0].evidence[0].locator, "pi://empty/entry/user-empty");
console.log(JSON.stringify({calls, firstContext: firstContext.message.customType}));
'''


class PiLifecycleTests(unittest.TestCase):
    """Run the shipped TypeScript against a fake Pi API and native branch."""

    @unittest.skipUnless(shutil.which("node"), "Node.js 24 is required for TS stripping")
    def test_first_turn_repeated_prompt_abort_and_branch_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "pi-harness.mjs"
            script.write_text(HARNESS, encoding="utf-8")
            result = subprocess.run([shutil.which("node"), "--experimental-strip-types",
                                     str(script), str(EXTENSION)], text=True,
                                    capture_output=True, check=True)
        output = json.loads(result.stdout)
        self.assertEqual("llmwiki-context", output["firstContext"])
        self.assertEqual(5, len([item for item in output["calls"] if item["action"] == "prompt"]))
        self.assertEqual(2, len([item for item in output["calls"] if item["action"] == "stop"]))


if __name__ == "__main__":
    unittest.main()
