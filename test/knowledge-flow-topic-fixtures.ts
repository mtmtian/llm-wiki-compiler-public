/** Real temporary wikis and evidence-bound publications for topic-page scenarios. */
import { mkdtemp, mkdir, rm, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { onTestFinished } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowClaim, FlowConfig } from "../extensions/knowledge-flow/types.js";

/** Independent accepted claims can describe separate aspects of one decision object. */
export function topicRecord(id: string, statements: string[], overrides: Partial<FlowClaim> = {}): PublicationRecord {
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a",
    projectId: "companion", projectLabel: "Companion", createdAt: "2026-09-17T00:00:00Z", originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds: [], review: { status: "accepted", model: "test" },
    claims: statements.map((text, index) => ({ text, quote: text, evidenceId: `e${index}`, title: "样例素材推广方案",
      topic: "样例素材推广", decisionObject: "样例项目首轮素材测试", slug: "sample-material-promotion", targetPageId: null,
      kind: "decision", status: "decided", useWhen: "启动样例首轮素材推广时", rationale: "保留目标和取舍", ...overrides })),
    evidence: statements.map((text, index) => ({ id: `e${index}`, kind: "user", text, sha256: sha256Text(text),
      originalSha256: sha256Text(text), locator: `knowledge-evidence://a/${sha256Text(text)}`, observedAt: "2026-09-17T00:00:00Z" })) } };
}

/** No real vault, runtime, model or network access is used by these fixtures. */
export async function topicFixture(): Promise<FlowConfig> {
  const root = await mkdtemp(path.join(tmpdir(), "wiki-topic-view-"));
  onTestFinished(() => rm(root, { recursive: true, force: true }));
  await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
  return { wikiRoot: root, stateDir: path.join(root, "state"), model: "test", maxProposals: 5, maxPendingPerProject: 10 };
}

/** Read observable page/source artifacts rather than implementation-private state. */
export async function topicFiles(config: FlowConfig, folder = "wiki/concepts"): Promise<Map<string, string>> {
  const directory = path.join(config.wikiRoot, folder);
  const files = await readdir(directory).catch(() => [] as string[]);
  return new Map(await Promise.all(files.sort().map(async name => [name, await readFile(path.join(directory, name), "utf8")] as const)));
}
