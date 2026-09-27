/** Given reviewed whole-page revisions, replay must be deterministic and fail closed. */
import { readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

function revisionRecord(id: string, body: string, pageId: string, basisHash: string | null,
  basisRecordIds: string[] = [], projectId = "example"): PublicationRecord {
  const text = "采用小批量发布并保留回滚";
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a",
    projectId, projectLabel: "Example", createdAt: "2026-09-17T00:00:00Z", originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds, review: { status: "accepted", model: "test" },
    claims: [{ text, quote: text, evidenceId: "e1", title: "发布策略", topic: "发布流程",
      decisionObject: "小批量发布", slug: "release", targetPageId: pageId, kind: "decision", status: "decided",
      useWhen: "发布新版本时", rationale: "降低回滚风险" }],
    evidence: [{ id: "e1", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text),
      locator: "knowledge-evidence://a/revision", observedAt: "2026-09-17T00:00:00Z" }],
    topicRevisions: [{ pageId, topicId: stableTopicId(projectId, "发布流程", "小批量发布"), title: "发布流程",
      topic: "发布流程", decisionObject: "小批量发布", basisHash, body, claimIndexes: [0] }] } };
}

const fixture = (): Promise<FlowConfig> => makeKnowledgeFlowConfig("wiki-revision-view-");

// Captured from release 1ec26e87b3af: stored revision bases cover serialized bytes.
const LEGACY_FIRST_REVISION_HASH = "605a0caa926b3fa5c594b3aee9071e72531b004d2d84ec3868598be736e9e854";

describe("whole-page topic revisions", () => {
  it.each([undefined, "semantic"] as const)("Given a pre-upgrade revision chain, When replayed in %s scope, Then frozen basis hashes still match", async (topicScope) => {
    const pageId = "concepts/example-release-abc123";
    const first = revisionRecord("a", "## 发布流程\n\n首版 {{claim:0}}", pageId, null);
    const seed = await fixture(); seed.topicScope = topicScope;
    expect((await materializeRecords(seed, [first])).conflicts).toEqual([]);
    const original = await readFile(path.join(seed.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect(sha256Text(original)).toBe(LEGACY_FIRST_REVISION_HASH);
    const second = revisionRecord("c", "## 发布流程\n\n后续 {{claim:0}}\n\n历史 ^[Example-2026-09-17-aaaaaaaaaaaa.md:5]",
      pageId, LEGACY_FIRST_REVISION_HASH, [first.id]);
    const replay = await fixture(); replay.topicScope = topicScope;
    expect((await materializeRecords(replay, [second, first])).conflicts).toEqual([]);
    const updated = await readFile(path.join(replay.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect(updated).toContain("后续");
    expect(updated).toContain("^[Example-2026-09-17-aaaaaaaaaaaa.md:5]");
    expect(updated).toContain("^[Example-2026-09-17-cccccccccccc.md:5]");
  });

  it("Given a new reviewed revision, When replayed, Then its claim citation and stable topic id are rendered", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const record = revisionRecord("a", "## 发布流程\n\n当前结论：{{claim:0}}", pageId, null);
    await materializeRecords(config, [record]);
    const body = await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect(body).toContain("knowledgeTopicId: " + stableTopicId("example", "发布流程", "小批量发布"));
    expect(body).toContain("当前结论"); expect(body).not.toContain("{{claim:0}}"); expect(body).toContain("^[Example-");
  });

  it("Given a historical-only revision, Then page authority stays historical", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const record = revisionRecord("a", "## 发布流程\n\n历史观察：{{claim:0}}", pageId, null);
    record.payload.claims[0].status = "historical";
    await materializeRecords(config, [record]);
    const body = await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect(body).toContain("status: historical");
  });

  it("Given an existing page hash, When a revision applies, Then old citations and evidence metadata remain byte-for-byte present", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const original = "---\ntitle: 旧发布流程\nsources:\n  - legacy.md\n---\n\n## 历史\n\n旧结论 ^[legacy.md:1]\n";
    await writeFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), original);
    const record = revisionRecord("a", "## 发布流程\n\n新结论 {{claim:0}}\n\n旧结论 ^[legacy.md:1]", pageId, sha256Text(original));
    await materializeRecords(config, [record]);
    const body = await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect(body).toContain("legacy.md:1"); expect(body).toContain("新结论"); expect(body).toContain("sources:");
  });

  it.each(["user", "assistant"] as const)("Given %s context approved across turns, Then every quote is materialized with provenance", async (kind) => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const record = revisionRecord("a", "## 发布流程\n\n{{claim:0}}", pageId, null);
    const supportText = "上一轮确认保留回滚";
    record.payload.claims[0].supportingQuotes = [{ evidenceId: "e2", quote: supportText }];
    record.payload.evidence.push({ id: "e2", kind, text: supportText, sha256: sha256Text(supportText), originalSha256: sha256Text(supportText), locator: "knowledge-evidence://a/support", observedAt: "2026-09-16T00:00:00Z" });
    await materializeRecords(config, [record]);
    const source = (await readdir(path.join(config.wikiRoot, "sources"))).find(name => name.endsWith(".md"))!;
    const sourceBody = await readFile(path.join(config.wikiRoot, "sources", source), "utf8");
    expect(sourceBody).toContain(supportText); expect(sourceBody).toContain("knowledge-evidence://a/support"); expect(sourceBody).toContain(sha256Text(supportText));
    const page = await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    expect((page.match(/\^\[/g) ?? []).length).toBeGreaterThanOrEqual(2);
  });

  it("Given an assistant-only decision, Then the whole revision is rejected before any page is written", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const record = revisionRecord("a", "## 发布流程\n\n{{claim:0}}", pageId, null);
    record.payload.evidence[0].kind = "assistant";
    await expect(materializeRecords(config, [record])).rejects.toThrow(/claim|evidence/i);
    expect(await readdir(path.join(config.wikiRoot, "wiki/concepts"))).toEqual([]);
  });

  it("Given independent revisions of one page, When neither observes the other, Then both are held", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const first = revisionRecord("a", "## A\n\n{{claim:0}}", pageId, null);
    const second = revisionRecord("c", "## C\n\n{{claim:0}}", pageId, null);
    const result = await materializeRecords(config, [first, second]);
    expect(result.conflicts.some(item => item.reason.includes("concurrent"))).toBe(true);
    expect((await readdir(path.join(config.wikiRoot, "wiki/concepts"))).filter(name => name.endsWith(".md"))).toEqual([]);
    expect((await readdir(path.join(config.wikiRoot, "sources")).catch(() => [])).filter(name => name.endsWith(".md"))).toEqual([]);
  });

  it("Given the same reviewed publication replayed twice, Then the second run is byte-identical and a dependent revision still applies", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const first = revisionRecord("a", "## 发布流程\n\n首版 {{claim:0}}", pageId, null);
    await materializeRecords(config, [first]);
    const before = await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    await materializeRecords(config, [first]);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`), "utf8")).toBe(before);
    const citation = (before.match(/\^\[[^\]]+\]/g) ?? [])[0] ?? "";
    const second = revisionRecord("c", `## 发布流程\n\n后续 {{claim:0}}\n\n保留历史 ${citation}`, pageId, sha256Text(before), [first.id]);
    const fresh = await fixture(); await writeFile(path.join(fresh.wikiRoot, "wiki", `${pageId}.md`), before);
    await materializeRecords(fresh, [first, second]);
    expect(await readFile(path.join(fresh.wikiRoot, "wiki", `${pageId}.md`), "utf8")).toContain("后续");
  });

  it("Given malformed revision input, Then no page is written", async () => {
    const config = await fixture(); const pageId = "concepts/example-release-abc123";
    const invalid = revisionRecord("a", "## 发布流程\n\n{{claim:1}}", pageId, null);
    await expect(materializeRecords(config, [invalid])).rejects.toThrow(/claim|revision/i);
    expect((await readdir(path.join(config.wikiRoot, "wiki/concepts"))).filter(name => name.endsWith(".md"))).toEqual([]);
  });

  it("Given one revision record targeting two pages with one bad basis, Then the whole record is held atomically", async () => {
    const config = await fixture(); const firstPage = "concepts/example-one"; const secondPage = "concepts/example-two";
    const firstBody = "---\ntitle: One\n---\n\nOne ^[old-one.md:1]\n"; const secondBody = "---\ntitle: Two\n---\n\nTwo ^[old-two.md:1]\n";
    await writeFile(path.join(config.wikiRoot, "wiki", `${firstPage}.md`), firstBody);
    await writeFile(path.join(config.wikiRoot, "wiki", `${secondPage}.md`), secondBody);
    const record = revisionRecord("a", `## One\n\n{{claim:0}}\n\nOne ^[old-one.md:1]`, firstPage, sha256Text(firstBody));
    const secondClaim = { ...record.payload.claims[0], text: "第二页也要保留回滚", quote: "第二页也要保留回滚", evidenceId: "e2", targetPageId: secondPage };
    record.payload.claims = [record.payload.claims[0], secondClaim];
    record.payload.evidence = [...record.payload.evidence, { ...record.payload.evidence[0], id: "e2", text: "第二页也要保留回滚", sha256: sha256Text("第二页也要保留回滚"), originalSha256: sha256Text("第二页也要保留回滚") }];
    record.payload.topicRevisions = [record.payload.topicRevisions![0], { ...record.payload.topicRevisions![0], pageId: secondPage, body: "## Two\n\n{{claim:1}}\n\nTwo ^[old-two.md:1]", claimIndexes: [1], basisHash: "d".repeat(64) }];
    const result = await materializeRecords(config, [record]);
    expect(result.conflicts.some(item => item.recordIds.includes(record.id))).toBe(true);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${firstPage}.md`), "utf8")).toBe(firstBody);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${secondPage}.md`), "utf8")).toBe(secondBody);
  });

  it("Given an ancestry-linked revision set, When records arrive in reverse order, Then replay bytes are identical", async () => {
    const seed = await fixture(); const pageId = "concepts/example-release-abc123";
    const first = revisionRecord("a", "## 发布流程\n\n首版 {{claim:0}}", pageId, null);
    await materializeRecords(seed, [first]);
    const firstBody = await readFile(path.join(seed.wikiRoot, "wiki", `${pageId}.md`), "utf8");
    const citation = (firstBody.match(/\^\[[^\]]+\]/g) ?? [])[0] ?? "";
    const second = revisionRecord("c", `## 发布流程\n\n后续 {{claim:0}}\n\n历史 ${citation}`, pageId, sha256Text(firstBody), [first.id]);
    const left = await fixture(); const right = await fixture();
    await materializeRecords(left, [first, second]); await materializeRecords(right, [second, first]);
    expect(await readFile(path.join(left.wikiRoot, "wiki", `${pageId}.md`), "utf8")).toBe(await readFile(path.join(right.wikiRoot, "wiki", `${pageId}.md`), "utf8"));
  });
});
