/** User-visible invariants for an optional Jev trial, using real temporary budget storage. */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import type { TaskContext } from "../src/context/task-types.js";
import { rerankJev } from "../extensions/knowledge-flow/jev-context.js";
import { trialStatus } from "../extensions/knowledge-flow/jev-ledger.js";
import { renderHookPack } from "../extensions/knowledge-flow/context.js";
import { unresponsiveHttpServer } from "./fixtures/unresponsive-http.js";

let directory: string;
beforeEach(async () => { directory = await mkdtemp(path.join(tmpdir(), "jev-trial-")); });
afterEach(async () => { await rm(directory, { recursive: true, force: true }); });
const settings = () => ({ stateDir: directory, jevContext: { enabled: true, budgetUsd: .02, expiresAt: "2030-01-01T00:00:00Z" } });
const pack = (): TaskContext => ({ version: 1, projectId: "project-a", status: "ok", complete: true,
  followUpPageIds: [], diagnostics: { scopedPages: 1, matchedSections: 2, warnings: [] },
  evidence: ["background", "answer"].map(section => ({ pageId: "concepts/owned", pageRevision: "revision",
    title: "Owned page", updatedAt: null, decisionObject: null, section, qualifications: "Historical only",
    text: `${section} ^[source.md:1-1]`, sources: [{ file: "source.md", start: 1, end: 1, text: "original evidence" }] })) });

function reply() {
  return new Response(JSON.stringify({ model: "jev-1.13.0", usage: { input_tokens: 400, output_tokens: 60 }, answers: {
    candidate_0: { type: "choice", choice: "supporting", probabilities: { direct: .1, supporting: .8, irrelevant: .1 } },
    candidate_1: { type: "choice", choice: "direct", probabilities: { direct: .95, supporting: .05, irrelevant: 0 } },
  } }));
}
const deps = (fetch: typeof globalThis.fetch) => ({ fetch, key: async () => "test-key" });

describe("Jev trial context", () => {
  it("Given disabled or empty retrieval, preserves rules without contacting a provider", async () => {
    const forbidden = deps(async () => { throw new Error("Provider must not be called"); });
    const original = pack();
    const disabled = await rerankJev(original, "question", { ...settings(), jevContext: { ...settings().jevContext, enabled: false } }, forbidden);
    expect(disabled.pack).toEqual(original);
    const empty = { ...original, evidence: [] };
    expect((await rerankJev(empty, "question", settings(), forbidden)).pack).toEqual(empty);
    expect(trialStatus(directory).requests).toBe(0);
  });

  it("Given valid native scores, puts the answer first without changing its sources", async () => {
    const original = pack();
    const result = await rerankJev(original, "question", settings(), deps(async (url, init) => {
      expect(url).toBe("https://api.typesafe.ai/v1/systemone");
      const body = JSON.parse(String(init?.body));
      expect(body.model).toBe("jev-1.13.0");
      expect(JSON.stringify(body)).not.toContain("original evidence");
      expect(JSON.stringify(body)).not.toContain("source.md");
      return reply();
    }));
    expect(result.status.mode).toBe("jev");
    expect(result.pack.evidence).toEqual([original.evidence[1], original.evidence[0]]);
    expect(trialStatus(directory).inputTokens).toBe(400);
  });

  it("Given provider failure, preserves the full baseline and pauses retries", async () => {
    const original = pack();
    const failed = await rerankJev(original, "question", settings(), deps(async () => new Response("unavailable", { status: 503 })));
    expect(failed.pack).toEqual(original);
    const input = { config: { wikiRoot: directory, maxContextChars: 2400 }, prompt: "question", allowedPageIds: [], seen: {} };
    expect(renderHookPack(input, failed.pack)).toEqual(renderHookPack(input, original));
    expect(failed.status.mode).toBe("rules");
    const paused = await rerankJev(original, "question", settings(), deps(async () => { throw new Error("cooldown bypassed"); }));
    expect(paused.status.reason).toBe("cooldown");
    expect(trialStatus(directory).requests).toBe(1);
  });

  it("Given spent credit, never calls the provider again even after another invocation", async () => {
    const original = pack();
    const exhausted = await rerankJev(original, "question", settings(), deps(async () => new Response("insufficient credit", { status: 402 })));
    expect(exhausted.pack).toEqual(original);
    expect(trialStatus(directory).stoppedReason).toBe("credit-exhausted");
    const next = await rerankJev(original, "question", settings(), deps(async () => { throw new Error("credit bypassed"); }));
    expect(next.status.reason).toBe("credit-exhausted");
  });

  it("Given a tiny budget or expired grant, preserves current rules without a billed request", async () => {
    const forbidden = deps(async () => { throw new Error("budget bypassed"); });
    const tiny = { ...settings(), jevContext: { ...settings().jevContext, budgetUsd: .000001 } };
    expect((await rerankJev(pack(), "question", tiny, forbidden)).status.reason).toBe("budget-exhausted");
    const expired = { ...settings(), jevContext: { ...settings().jevContext, expiresAt: "2000-01-01T00:00:00Z" } };
    expect((await rerankJev(pack(), "question", expired, forbidden)).status.reason).toBe("expired");
    expect(trialStatus(directory).requests).toBe(0);
  });

  it("Given a substituted model or incomplete score set, cannot replace any baseline evidence", async () => {
    const original = pack();
    const response = new Response(JSON.stringify({ model: "ling-3.0", answers: {} }));
    const result = await rerankJev(original, "question", settings(), deps(async () => response));
    expect(result.pack).toEqual(original);
    expect(result.status.reason).toBe("invalid-response");
  });

  it("Given an unresponsive HTTP provider, returns baseline within the trial deadline", async () => {
    const server = await unresponsiveHttpServer();
    const started = Date.now();
    try {
      const original = pack();
      const result = await rerankJev(original, "question", settings(),
        deps(async (_url, init) => fetch(server.url, init)), 250);
      expect(result.pack).toEqual(original);
      expect(result.status.reason).toBe("transport-failure");
      expect(Date.now() - started).toBeLessThan(1500);
    } finally { await server.close(); }
  });
});
