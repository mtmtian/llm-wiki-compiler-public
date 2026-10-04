/** Isolated knowledge-flow roots and materialized page readers for replay tests. */
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { onTestFinished } from "vitest";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";

/** Create an isolated concepts root shared by knowledge-flow materializer tests. */
export async function makeKnowledgeFlowConfig(prefix = "wiki-topic-view-"): Promise<FlowConfig> {
  const root = await mkdtemp(path.join(tmpdir(), prefix));
  onTestFinished(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
  return { wikiRoot: root, stateDir: path.join(root, "state"), model: "test", maxProposals: 5, maxPendingPerProject: 10 };
}

/** Replay records and read their resulting pages, distinguishing absent pages from I/O failures. */
export async function materializeKnowledgeFlow(config: FlowConfig, records: PublicationRecord[]) {
  const result = await materializeRecords(config, records);
  const read = async (pageId: string) => {
    try { return await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8"); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
  };
  return { result, read };
}
