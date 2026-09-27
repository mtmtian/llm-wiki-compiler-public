/** Two-machine contribution, publication, evidence authority and duplicate guards. */
import { mkdtemp, mkdir, readdir, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { extractClaims } from "../extensions/knowledge-flow/extract.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowClaim, FlowConfig, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
const text = "Use stable project IDs";
const claim: FlowClaim = { text, quote: text, evidenceId: "e1", title: "Project identity", topic: "identity", slug: "identity",
  targetPageId: null, kind: "decision", status: "decided", useWhen: "Sharing project knowledge", rationale: "Avoid mixing projects", replacementIntent: false };
const provider = { toolCall: async () => JSON.stringify({ claims: [claim] }) } as unknown as LLMProvider;

async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), "knowledge-sharing-"));
  roots.push(root);
  await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
  const config: FlowConfig = { wikiRoot: root, stateDir: path.join(root, "local"), model: "test",
    maxProposals: 5, maxPendingPerProject: 10, machineId: "a", publishEnabled: false,
    exchange: { root: path.join(root, "exchange"), publisherMachineId: "b", participants: ["a", "b"] }, provider, reviewer: provider };
  const job: FlowJob = { id: "job-a", projectId: "project", projectLabel: "Project", sessionId: "s", turnId: "t", cwd: root,
    createdAt: "2026-09-15T00:00:00Z", prompt: "Decision", lastAssistant: "Private assistant tail", allowedPageIds: [],
    evidence: [{ id: "e1", kind: "user", text: `${text}\nPrivate unrelated discussion`, locator: "turn:t", observedAt: "2026-09-15T00:00:00Z",
      sha256: sha256Text(`${text}\nPrivate unrelated discussion`) }] };
  return { root, config, job };
}

const accepted = { extract: async () => [claim], review: async () => [{ index: 0, decision: "accept" as const, reason: "supported", conflictingPageIds: [] }] };

describe("shared project accumulation", () => {
  it("contributes exact quotes without writing sources, pages or full conversation", async () => {
    const { root, config, job } = await fixture();
    const result = await processJob(job, config, accepted);
    expect(result.status).toBe("submitted");
    expect(result.contribution?.evidence[0]).toMatchObject({ text, originalSha256: job.evidence[0].sha256 });
    expect(JSON.stringify(result.contribution)).not.toContain("Private");
    expect(await readdir(path.join(root, "wiki/concepts"))).toEqual([]);
    await expect(readdir(path.join(root, "sources"))).rejects.toThrow();
    expect(await processJob(job, config, accepted)).toEqual(result);
  });

  it("re-reviews incoming quotes and suppresses a duplicate from another machine", async () => {
    const { root, config, job } = await fixture();
    const sent = await processJob(job, config, accepted);
    const publisher = { ...config, stateDir: path.join(root, "publisher"), machineId: "b", publishEnabled: true };
    const incoming = { ...job, id: "exchange-a", evidence: sent.contribution!.evidence, submittedClaims: sent.contribution!.claims };
    const first = await processJob(incoming, publisher, accepted);
    expect(first.status).toBe("published");
    const duplicate = await processJob({ ...incoming, id: "exchange-b", allowedPageIds: first.publishedPageIds }, publisher, accepted);
    expect(duplicate.status).toBe("empty");
    expect(await readdir(path.join(root, "sources"))).toHaveLength(1);
    expect(await readFile(path.join(root, "sources", (await readdir(path.join(root, "sources")))[0]), "utf8")).toContain(job.evidence[0].sha256);
  });

  it("holds an incoming conflict without modifying the shared wiki", async () => {
    const { root, config, job } = await fixture();
    const result = await processJob({ ...job, submittedClaims: [claim] }, { ...config, machineId: "b", publishEnabled: true },
      { ...accepted, review: async () => [{ index: 0, decision: "needs_review", reason: "contradiction", conflictingPageIds: [] }] });
    expect(result.status).toBe("needs_review");
    expect(await readdir(path.join(root, "wiki/concepts"))).toEqual([]);
  });

  it("never accepts assistant evidence even when the model asks to publish it", async () => {
    const { job } = await fixture();
    job.evidence[0].kind = "assistant";
    expect(await extractClaims(provider, job, new Map(), 5)).toEqual([]);
  });

  it("Given v2 publishing on either machine, Then only reviewed quote records leave the pipeline and no shared compiler state changes", async () => {
    const { root, config, job } = await fixture();
    for (const machineId of ["a", "b"]) {
      const next = { ...config, machineId, publishEnabled: true, stateDir: path.join(root, machineId),
        exchange: { ...config.exchange!, protocolVersion: 2 } };
      const result = await processJob({ ...job, id: `v2-${machineId}` }, next, accepted);
      expect(result.status).toBe("submitted");
      expect(result.contribution?.claims[0].text).toBe(text);
    }
    expect(await readdir(path.join(root, "wiki/concepts"))).toEqual([]);
    await expect(readdir(path.join(root, "sources"))).rejects.toThrow();
  });
});
