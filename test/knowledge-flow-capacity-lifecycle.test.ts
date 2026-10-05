/**
 * Capacity is an admission condition, not an outcome of evidence review.
 * These boundary checks use no model: empty evidence can run only once a
 * logical review slot is available, including interrupted retry handoffs.
 */
import { mkdir, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";
import { job } from "./knowledge-flow-consolidation-fixtures.js";

/** Persist synthetic reviews, retaining their raw count while testing admission. */
async function fixture(records: Record<string, object>, limit = 1) {
  const config = await makeKnowledgeFlowConfig("capacity-lifecycle-");
  config.maxPendingPerProject = limit;
  const reviewDir = path.join(config.stateDir, "review");
  await mkdir(reviewDir, { recursive: true });
  for (const [id, record] of Object.entries(records)) {
    await writeFile(path.join(reviewDir, `${id}.json`), JSON.stringify({ jobId: id, projectId: "companion", ...record }));
  }
  return { config, reviewDir, input: { ...job(), evidence: [], allowedPageIds: [] } };
}

it("Given repeated full capacity, When a slot opens, Then the same input runs without a terminal audit", async () => {
  const { config, reviewDir, input } = await fixture({ old: {} });
  for (let attempt = 0; attempt < 3; attempt += 1) {
    expect(await processJob(input, config)).toMatchObject({ status: "deferred", reviewCount: 0, error: "review queue is full" });
  }
  await expect(readdir(path.join(config.stateDir, "audit"))).rejects.toThrow();
  expect(await readdir(reviewDir)).toEqual(["old.json"]);
  await rm(path.join(reviewDir, "old.json"));
  expect(await processJob(input, config)).toMatchObject({ status: "empty", reviewCount: 0 });
});

it("Given an interrupted chain handoff, Then its ancestor and successor consume one slot", async () => {
  const { config, reviewDir, input } = await fixture({ old: {}, retry: { reviewRetryOf: "old" }, newer: { reviewRetryOf: "retry" } }, 2);
  expect((await processJob(input, config)).status).toBe("empty");
  expect(await readdir(reviewDir)).toHaveLength(3);
});

it("Given a retry replacing a chain, Then excludes that one logical item from its own admission", async () => {
  const { config, input } = await fixture({ old: {}, retry: { reviewRetryOf: "old" } });
  expect((await processJob({ ...input, reviewRetryOf: "old" }, config)).status).toBe("empty");
});

it.each([
  { a: { reviewRetryOf: "b" }, b: { reviewRetryOf: "c" }, c: { reviewRetryOf: "a" } },
  { a: {}, b: { reviewRetryOf: "a" }, c: { reviewRetryOf: "a" } },
  { a: { reviewRetryOf: "a" }, b: { reviewRetryOf: "a" }, c: { reviewRetryOf: "b" } },
])("Given cyclic or branching lineage, Then counts every unresolved record conservatively", async records => {
  const { config, input } = await fixture(records, 3);
  expect((await processJob(input, config)).status).toBe("deferred");
});

it("Given a cross-project or forged lineage, Then cannot suppress a project review slot", async () => {
  const { config, input } = await fixture({
    foreign: { projectId: "other" }, local: { reviewRetryOf: "foreign" },
    forged: { jobId: "local", reviewRetryOf: "local" },
  }, 2);
  expect((await processJob(input, config)).status).toBe("deferred");
});

it("Given a child without its own identity, Then its claimed parent does not hide another review", async () => {
  const { config, input } = await fixture({ old: {}, unidentified: { jobId: undefined, reviewRetryOf: "old" } }, 2);
  expect((await processJob(input, config)).status).toBe("deferred");
});
