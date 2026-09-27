/** Immutable publication replay must preserve hashes from pages written by older renderers. */
import { readFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { buildFrontmatter, parseFrontmatter } from "../src/utils/markdown.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

const pageId = "concepts/legacy-summary";

function revisionRecord(id: string, body: string, basisHash: string | null, basisRecordIds: string[] = []): PublicationRecord {
  const text = `采用 ${id} 版本发布并保留回滚`;
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a",
    projectId: "example", projectLabel: "Example", createdAt: "2026-09-17T00:00:00Z", originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds, review: { status: "accepted", model: "test" },
    claims: [{ text, quote: text, evidenceId: "e1", title: "发布流程", topic: "发布流程", decisionObject: "小批量发布",
      slug: "legacy-summary", targetPageId: pageId, kind: "decision", status: "decided", useWhen: "发布时", rationale: "保留回滚" }],
    evidence: [{ id: "e1", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text),
      locator: `knowledge-evidence://a/${id}`, observedAt: "2026-09-17T00:00:00Z" }],
    topicRevisions: [{ pageId, topicId: stableTopicId("example", "发布流程", "小批量发布"), title: "发布流程",
      topic: "发布流程", decisionObject: "小批量发布", basisHash, body, claimIndexes: [0] }] } };
}

function legacySummary(body: string): string {
  return body.split(/\n\s*\n/).find(line => line.trim() && !line.trim().startsWith("#"))?.trim().slice(0, 240) ?? body.trim().slice(0, 240);
}

describe("legacy summary publication replay", () => {
  it("Given B hashes the old A page bytes, When immutable A and B replay on a fresh root, Then B still applies", async () => {
    const first = revisionRecord("a", `## 发布流程\n\n${"长".repeat(230)} {{claim:0}}`, null);
    const seed = await makeKnowledgeFlowConfig("wiki-legacy-summary-seed-");
    await materializeRecords(seed, [first]);
    const rendered = await readFile(path.join(seed.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    const parsed = parseFrontmatter(rendered);
    const legacyPage = `${buildFrontmatter({ ...parsed.meta, summary: legacySummary(parsed.body) })}\n${parsed.body}`;
    const citation = parsed.body.match(/\^\[[^\]]+\]/)?.[0];
    expect(citation).toBeDefined();
    const second = revisionRecord("c", `## 发布流程\n\n后续结论 {{claim:0}}\n\n保留首次证据 ${citation}`, sha256Text(legacyPage), [first.id]);
    const replay = await makeKnowledgeFlowConfig("wiki-legacy-summary-replay-");

    const result = await materializeRecords(replay, [first, second]);

    expect(result.conflicts).toEqual([]);
    expect(await readFile(path.join(replay.wikiRoot, "wiki", `${pageId}.md`), "utf8")).toContain("后续结论");
  });
});
