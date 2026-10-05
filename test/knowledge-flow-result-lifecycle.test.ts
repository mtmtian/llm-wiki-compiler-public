/**
 * Session outcomes describe the work still required, independently of whether a
 * model call returned. Technical failures stay visible without consuming human
 * review capacity; only unresolved intent creates a durable review item.
 */
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { sha256Text } from "../src/connectors/hash.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

/** Exercise the actual session pipeline with an isolated wiki and scripted models. */
async function fixture(review: unknown = accepted(), edited = draft()) {
  const calls: string[] = [];
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: edited, knowledge_topic_review: review },
    (_system, name) => calls.push(name));
  const page = path.join(runtime.wikiRoot, "wiki", `${pageId}.md`);
  await mkdir(path.dirname(page), { recursive: true });
  await writeFile(page, original);
  return { runtime, input: job(), calls, page };
}

it("Given omitted review coverage, Then records a permanent technical failure without a human hold", async () => {
  const { runtime, input, calls, page } = await fixture({ ...accepted(), checkedClaimIndexes: [],
    claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "supported" }] });
  runtime.knowledgeLedger = true;
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
  expect(result.error).toContain("review claim coverage mismatch");
  expect(result.contribution).toBeUndefined();
  expect(result.ledgerContribution).toBeUndefined();
  await expect(readdir(path.join(runtime.stateDir, "review"))).rejects.toThrow();
  expect(await readFile(page, "utf8")).toBe(original);
  expect(await processJob(input, runtime)).toEqual(result);
  expect(calls).toEqual(["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review"]);
});

it("Given unresolved user intent, Then preserves a human hold and its explicit retry lineage", async () => {
  const { runtime, input } = await fixture({ ...accepted(), decision: "needs_review", reason: "replacement needs user intent" });
  input.reviewRetryOf = "original-hold";
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "needs_review", reviewCount: 1 });
  expect(result.retryable).toBeUndefined();
  expect(JSON.parse(await readFile(result.reviewFile!, "utf8"))).toMatchObject({
    jobId: input.id, reviewRetryOf: "original-hold", projectId: input.projectId,
  });
  expect(await processJob(input, runtime)).toEqual(result);
});

it("Given an explicitly uncertain claim, Then preserves the unresolved user decision instead of calling it invalid output", async () => {
  const edited = draft(); edited.claims[0].status = "uncertain";
  const { runtime, input } = await fixture(accepted(), edited);
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "needs_review", reviewCount: 1 });
  expect(result.retryable).toBeUndefined();
  expect(result.contribution).toBeUndefined();
  expect(result.reviewFile).toBeDefined();
});

it("Given malformed model output, Then a durable failure prevents repeated calls to the same attempt", async () => {
  const { runtime, input, calls } = await fixture();
  runtime.provider!.toolCall = async () => { calls.push("malformed"); return "{"; };
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
  expect(await processJob(input, runtime)).toEqual(result);
  expect(calls).toEqual(["malformed"]);
  await expect(readdir(path.join(runtime.stateDir, "review"))).rejects.toThrow();
});

it("Given corrupt frozen output, Then requires a new attempt instead of treating it as human ambiguity", async () => {
  const { runtime, input, calls } = await fixture();
  const folder = path.join(runtime.stateDir, "consolidation", sha256Text(input.id));
  await mkdir(folder, { recursive: true });
  await writeFile(path.join(folder, "knowledge_topic_plan.json"), "{");
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
  expect(calls).toEqual([]);
  expect(await processJob(input, runtime)).toEqual(result);
});

it("Given a temporary provider failure, Then keeps the existing bounded retry path available", async () => {
  const { runtime, input } = await fixture();
  const toolCall = runtime.provider!.toolCall;
  runtime.provider!.toolCall = async () => { throw new Error("temporary transport outage"); };
  const first = await processJob(input, runtime);
  expect(first).toMatchObject({ status: "error", error: "temporary transport outage" });
  expect(first.retryable).toBeUndefined();
  await expect(readdir(path.join(runtime.stateDir, "audit"))).rejects.toThrow();
  runtime.provider!.toolCall = toolCall;
  expect((await processJob(input, runtime)).status).toBe("submitted");
});

it("Given a temporary review failure, Then reuses the frozen draft and retries only the failed call", async () => {
  let unavailable = true;
  const { runtime, input, calls } = await fixture(() => {
    if (unavailable) { unavailable = false; throw new Error("review transport unavailable"); }
    return accepted();
  });
  const first = await processJob(input, runtime);
  expect(first).toMatchObject({ status: "error", error: "review transport unavailable" });
  expect(first.retryable).toBeUndefined();
  expect((await processJob(input, runtime)).status).toBe("submitted");
  expect(calls).toEqual(["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review", "knowledge_topic_review"]);
});

it("Given accepted claims in a rejected page, Then technical failure retains the gated ledger and audit recovery", async () => {
  const { runtime, input } = await fixture({ ...accepted(), decision: "reject", reason: "unsupported page prose",
    claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "this claim is supported" }] });
  runtime.knowledgeLedger = true;
  const result = await processJob(input, runtime);
  expect(result).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
  expect(result.ledgerContribution?.claims.map(claim => claim.text)).toEqual([input.prompt]);
  expect(result.ledgerContribution?.evidence.map(evidence => evidence.text)).toEqual([input.prompt]);
  expect(result.contribution).toBeUndefined();
  expect(await processJob(input, runtime)).toEqual(result);
  await expect(readdir(path.join(runtime.stateDir, "review"))).rejects.toThrow();
});
