/**
 * An explicit retry replaces one pending slot without bypassing evidence gates.
 * These tests exercise the real pipeline's capacity decision with an empty
 * evidence set, so no provider or publication can run accidentally.
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import type { FlowJob } from "../extensions/knowledge-flow/types.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

/** Build a project at its review cap with one original held item. */
async function fixture() {
  const config = await makeKnowledgeFlowConfig("review-retry-");
  config.maxPendingPerProject = 1;
  await mkdir(path.join(config.stateDir, "review"), { recursive: true });
  await writeFile(path.join(config.stateDir, "review/old.json"), JSON.stringify({ projectId: "project" }));
  const job: FlowJob = { id: "retry", projectId: "project", projectLabel: "Project", sessionId: "session",
    turnId: "turn", cwd: config.wikiRoot, createdAt: "2026-09-21T00:00:00Z", prompt: "", lastAssistant: "",
    evidence: [], allowedPageIds: [] };
  return { config, job };
}

it("allows an explicit replacement at capacity without publishing empty evidence", async () => {
  const { config, job } = await fixture();
  const result = await processJob({ ...job, reviewRetryOf: "old" }, config);
  expect(result).toMatchObject({ status: "empty", publishedPageIds: [] });
});

it("still blocks a new job or a retry referring to no existing hold", async () => {
  const { config, job } = await fixture();
  const result = await processJob({ ...job, reviewRetryOf: "missing" }, config);
  expect(result).toMatchObject({ status: "deferred", reviewCount: 0, error: "review queue is full" });
});
