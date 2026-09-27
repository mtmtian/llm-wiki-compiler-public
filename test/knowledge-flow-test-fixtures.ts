import { mkdir, mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { onTestFinished } from "vitest";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";

/** Create an isolated concepts root shared by knowledge-flow materializer tests. */
export async function makeKnowledgeFlowConfig(prefix: string): Promise<FlowConfig> {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  onTestFinished(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
  return { wikiRoot: root, stateDir: path.join(root, "state"), model: "test", maxProposals: 5, maxPendingPerProject: 10 };
}
