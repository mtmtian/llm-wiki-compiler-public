/**
 * Live smoke test for the installed Codex CLI.
 *
 * This is intentionally the only test that crosses the real Codex process and
 * subscription boundary. Run it explicitly with RUN_CODEX_LIVE_SMOKE=1;
 * ordinary local and PR tests must not depend on a paid model or login state.
 * When opted in, an installed-but-unauthenticated CLI is an actionable failure.
 */

import { execFileSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { CodexAgentProvider } from "../src/providers/codex-agent.js";

/** True when literal PATH lookup can launch the real installed CLI. */
function hasCodexBinary(): boolean {
  try {
    execFileSync("codex", ["--version"], { stdio: "ignore", timeout: 5_000 });
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ENOENT";
  }
}

describe("CodexAgentProvider live smoke", () => {
  it.skipIf(process.env.RUN_CODEX_LIVE_SMOKE !== "1" || !hasCodexBinary())("invokes the real installed codex exec binary", async () => {
    const provider = new CodexAgentProvider(undefined, { timeoutMs: 120_000 });
    const result = await provider.complete(
      "Return the requested literal text and nothing else.",
      [{ role: "user", content: "Reply exactly: codex-agent-smoke-ok" }],
      32,
    );
    expect(result.trim()).toBe("codex-agent-smoke-ok");
  }, 130_000);
});
