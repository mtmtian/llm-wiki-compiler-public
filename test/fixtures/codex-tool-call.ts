/** Invoke a tool through the Codex process adapter with a controlled fake CLI. */
import path from "node:path";
import { afterEach, vi } from "vitest";
import type { LLMTool } from "../../src/utils/provider.js";
import { CodexAgentProvider } from "../../src/providers/codex-agent.js";
import { installFakeCodex, type FakeCodex } from "./fake-codex.js";

const fakes: FakeCodex[] = [];

afterEach(async () => {
  vi.unstubAllEnvs();
  await Promise.all(fakes.splice(0).map(fake => fake.cleanup()));
});

/** Return parsed output and the exact schema delivered to the Codex process. */
export async function codexToolCall(tool: LLMTool, output: unknown, system: string, message = system) {
  const fake = await installFakeCodex({ toolOutput: output });
  fakes.push(fake);
  vi.stubEnv("PATH", `${fake.binDir}${path.delimiter}${process.env.PATH ?? ""}`);
  const raw = await new CodexAgentProvider().toolCall(system, [{ role: "user", content: message }], [tool], 100);
  const [call] = await fake.calls();
  return { output: JSON.parse(raw) as unknown, schema: call.schema };
}
