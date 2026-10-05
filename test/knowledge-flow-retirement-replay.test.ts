/** Replay tests keep automatic citation retirement's evidence basis deterministic. */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { stableClaimMarkers } from "./knowledge-flow-consolidation-fixtures.js";
import { validateRetirementReferences } from "../extensions/knowledge-flow/citation-retirement.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowConfig, FlowJob } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { LLMProvider } from "../src/utils/provider.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { readState } from "../src/utils/state.js";
import { topicFiles, topicFixture } from "./knowledge-flow-topic-fixtures.js";

const pageId = "concepts/replay";
const topicId = "d".repeat(64);
const project = { topic: "样例素材推广", decisionObject: "样例项目首轮素材测试" };

function revisionRecord(id: string, body: string, basisHash: string | null, basisRecordIds: string[] = [], retirement?: {
  citation: string; reason: string; replacement: string;
}): PublicationRecord {
  const text = `采用 ${id} 批次发布并保留回滚`;
  const record: PublicationRecord = { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a",
    projectId: "companion", projectLabel: "Companion", createdAt: "2026-09-17T00:00:00Z", originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds, review: { status: "accepted", model: "test" },
    claims: [{ text, quote: text, evidenceId: "e0", title: "发布方案", ...project, slug: "replay", targetPageId: pageId,
      kind: "decision", status: "decided", useWhen: "发布时", rationale: "保留回滚" }],
    evidence: [{ id: "e0", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text),
      locator: `knowledge-evidence://a/${sha256Text(text)}`, observedAt: "2026-09-17T00:00:00Z" }],
    topicRevisions: [{ pageId, topicId, title: "发布方案", ...project, basisHash, body, claimIndexes: [0],
      ...(retirement ? { citationRetirements: [retirement] } : {}) }] } };
  return record;
}

function sourceCitation(id: string): string {
  return `^[Companion-2026-09-17-${id.repeat(12)}.md:5]`;
}

function consolidationJob(url: string): FlowJob {
  const text = "用户确认保留发布方案";
  return { id: "replay-unquoted-url", projectId: "companion", projectLabel: "Companion", sessionId: "s", turnId: "t",
    cwd: "/tmp/project", createdAt: "2026-09-17T00:00:00Z", prompt: text, lastAssistant: "", allowedPageIds: [pageId],
    evidence: [{ id: "e0", kind: "user", text, sha256: sha256Text(text), locator: "codex://s/t", observedAt: "2026-09-17T00:00:00Z" },
      { id: "e1", kind: "user", text: `外部记录 ${url}`, sha256: sha256Text(`外部记录 ${url}`), locator: "codex://s/t", observedAt: "2026-09-17T00:00:00Z" }] };
}

function unquotedRetirementDraft(url: string): TopicDraft {
  const quote = "用户确认保留发布方案";
  return { claims: [{ text: quote, evidenceId: "e0", quote, title: "发布方案", topic: "样例素材推广",
    decisionObject: "样例项目首轮素材测试", slug: "replay", targetPageId: pageId, kind: "decision", status: "decided",
    useWhen: "发布时", rationale: "用户明确确认", replacementIntent: false, supportingQuotes: [] }],
    pages: [{ pageId, body: `## 发布方案\n\n用户确认保留发布方案。{{claim:0}}\n\n外部记录 ${url}`, claimIndexes: [0],
      citationRetirements: [{ citation: "^[legacy.md:1]", reason: "过程记录由外部 PR 承接", replacement: url }] }], summary: "保留方案并记录外部过程链接" };
}

function consolidationConfig(base: FlowConfig, url: string): FlowConfig {
  const unsupported = async (): Promise<never> => { throw new Error("unexpected provider operation"); };
  const draft = unquotedRetirementDraft(url);
  const quoteCatalog = buildCorrectionEvidence(consolidationJob(url).evidence);
  const provider: LLMProvider = { complete: unsupported, stream: unsupported, embed: unsupported,
    toolCall: async (_system, messages, tools): Promise<string> => {
      if (tools[0].name === "knowledge_topic_plan") return JSON.stringify({ summary: "保留发布方案", disposition: "edit", reason: "同一对象更新",
        pages: [{ action: "update", targetPageId: pageId, title: "发布方案", topic: "样例素材推广", decisionObject: "样例项目首轮素材测试", reason: "同一对象" }] });
      if (tools[0].name === "knowledge_topic_edit") {
        const request = JSON.parse(messages[0].content) as { correction?: { previousDraft: Record<string, any> } };
        if (!request.correction) return JSON.stringify(quoteBoundDraft(draft, quoteCatalog));
        return JSON.stringify(correctionPatch(draft, request.correction.previousDraft));
      }
      if (tools[0].name === "knowledge_topic_review") return JSON.stringify({ decision: "accept", reason: "来源已核验",
        checkedClaimIndexes: [0], checkedPageIds: [pageId], checkedRetiredCitations: ["^[legacy.md:1]"] });
      throw new Error(`unexpected tool ${tools[0].name}`);
    },
  };
  return { ...base, provider, reviewer: provider };
}

function quoteBoundDraft(draft: TopicDraft, catalog: ReturnType<typeof buildCorrectionEvidence>): Record<string, unknown> {
  return { ...draft, claims: draft.claims.map(claim => {
    const option = catalog.find(item => item.id === claim.evidenceId)?.quoteOptions.find(item => item.quote === claim.quote);
    const { evidenceId: _evidenceId, quote: _quote, topic: _topic, decisionObject: _decisionObject,
      supportingQuotes, ...fields } = claim;
    return { ...fields, quoteId: option?.quoteId ?? "unknown-quote", supportingQuotes: (supportingQuotes ?? []).map(item => {
      const support = catalog.find(source => source.id === item.evidenceId)?.quoteOptions.find(candidate => candidate.quote === item.quote);
      return { quoteId: support?.quoteId ?? "unknown-quote" };
    }) };
  }) };
}

function correctionPatch(draft: TopicDraft, previous: Record<string, any>): Record<string, unknown> {
  const claims = previous.claims as Array<Record<string, any>>;
  const pages = draft.pages.map(page => ({ pageId: page.pageId, body: stableClaimMarkers(page.body, claims),
    claimIds: page.claimIndexes.map(index => claims[index]?.claimId ?? `c${index}`),
    ...(page.citationRetirements?.length ? { citationRetirements: page.citationRetirements.map(item => ({
      ...item, replacement: stableClaimMarkers(item.replacement, claims),
    })) } : {}) }));
  return { claimUpdates: [], droppedClaimIds: [], pages, summary: draft.summary };
}

describe("reviewed citation retirement replay", () => {
  it.each(["", "summary: Truncated ^[missing-source.md:\n"])("reads baseline source URLs without treating metadata as evidence (%#)", async summary => {
    const config = await topicFixture(); const oldCitation = "^[legacy.md:1]";
    const url = "https://example.com/records/pr-9";
    const previous = `---\ntitle: 旧发布\n${summary}projectId: companion\n---\n\n旧过程 ${oldCitation}\n`;
    await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "replay.md"), previous);
    await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
    await writeFile(path.join(config.wikiRoot, "sources", "legacy.md"), `原始记录：${url}\n`);
    const record = revisionRecord("a", `## 发布方案\n\n{{claim:0}}\n\n替代记录 ${url}`, sha256Text(previous), [],
      { citation: oldCitation, reason: "过程记录由外部 PR 承接", replacement: url });

    await materializeRecords(config, [record]);

    const body = await readFile(path.join(config.wikiRoot, "wiki", "concepts", "replay.md"), "utf8");
    expect(body).toContain(url); expect(body).not.toContain(oldCitation);
  });

  it("keeps a baseline intact when retired provenance cannot be mapped to its original publication", async () => {
    const config = await topicFixture(); const oldCitation = "^[legacy.md:1]";
    const url = "https://example.com/records/pr-9";
    const previous = `---\ntitle: 旧发布\nprojectId: companion\nsources: [legacy.md]\nknowledgePublicationRefs: ["${"f".repeat(64)}:0"]\nknowledgeClaimIds: ["${"e".repeat(64)}"]\n---\n\n旧过程 ${oldCitation}\n`;
    const file = path.join(config.wikiRoot, "wiki", "concepts", "replay.md");
    await writeFile(file, previous);
    await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
    const originalSource = `原始记录：${url}\n`;
    await writeFile(path.join(config.wikiRoot, "sources", "legacy.md"), originalSource);
    const record = revisionRecord("a", `## 发布方案\n\n{{claim:0}}\n\n替代记录 ${url}`, sha256Text(previous), [],
      { citation: oldCitation, reason: "过程记录由外部 PR 承接", replacement: url });

    await expect(materializeRecords(config, [record])).rejects.toThrow(/provenance.*needs review/);

    expect(await readFile(file, "utf8")).toBe(previous);
    expect(await topicFiles(config, "sources")).toEqual(new Map([["legacy.md", originalSource]]));
  });

  it("retains a shared claim identity while a second publication still supports it", async () => {
    const first = revisionRecord("a", "## 发布方案\n\n{{claim:0}}", null);
    const initial = await topicFixture(); await materializeRecords(initial, [first]);
    const firstBody = (await topicFiles(initial)).get("replay.md")!;
    const second = revisionRecord("b", `${parseFrontmatter(firstBody).body}\n\n同一决定 {{claim:0}}`, sha256Text(firstBody), [first.id]);
    second.payload.claims = structuredClone(first.payload.claims);
    second.payload.evidence = structuredClone(first.payload.evidence);
    const middle = await topicFixture(); await materializeRecords(middle, [first, second]);
    const secondBody = (await topicFiles(middle)).get("replay.md")!;
    const third = revisionRecord("c", `## 发布方案\n\n保留决定 ${sourceCitation("b")}\n\n新约束 {{claim:0}}`,
      sha256Text(secondBody), [first.id, second.id], { citation: sourceCitation("a"), reason: "移除重复来源", replacement: sourceCitation("b") });
    const result = await topicFixture(); await materializeRecords(result, [first, second, third]);

    const meta = parseFrontmatter((await topicFiles(result)).get("replay.md")!).meta;
    const sharedId = parseFrontmatter(secondBody).meta.knowledgeClaimIds as string[];
    expect(meta.knowledgeClaimIds).toEqual(expect.arrayContaining(sharedId));
    expect(meta.knowledgePublicationRefs).toEqual([`${second.id}:0`, `${third.id}:0`]);
    expect((await topicFiles(result, "sources")).size).toBe(2);
  });

  it("removes only the retired page owner from a source bundle still used by another topic", async () => {
    const first = revisionRecord("a", "## 发布方案\n\n{{claim:0}}", null);
    const otherClaim = { ...first.payload.claims[0], topic: "存储", decisionObject: "磁盘备份", slug: "storage",
      targetPageId: "concepts/storage", text: "备份保留七天", quote: "备份保留七天", evidenceId: "e1" };
    first.payload.claims.push(otherClaim);
    first.payload.evidence.push({ ...first.payload.evidence[0], id: "e1", text: otherClaim.quote,
      sha256: sha256Text(otherClaim.quote), originalSha256: sha256Text(otherClaim.quote) });
    first.payload.topicRevisions!.push({ ...first.payload.topicRevisions![0], pageId: "concepts/storage", topicId: "f".repeat(64),
      title: "备份策略", topic: otherClaim.topic, decisionObject: otherClaim.decisionObject!, body: "## 备份\n\n{{claim:1}}", claimIndexes: [1] });
    const initial = await topicFixture(); await materializeRecords(initial, [first]);
    const previous = (await topicFiles(initial)).get("replay.md")!;
    const second = revisionRecord("b", "## 发布方案\n\n{{claim:0}}", sha256Text(previous), [first.id],
      { citation: sourceCitation("a"), reason: "更新此主题的证据", replacement: "{{claim:0}}" });
    const result = await topicFixture(); await materializeRecords(result, [first, second]);

    const sourceName = "Companion-2026-09-17-aaaaaaaaaaaa.md";
    expect((await readState(result.wikiRoot)).sources[sourceName].concepts).toEqual(["storage"]);
    expect((await topicFiles(result, "sources")).get(sourceName)).toBe((await topicFiles(initial, "sources")).get(sourceName));
    expect((await topicFiles(result)).get("storage.md")).toBe((await topicFiles(initial)).get("storage.md"));
  });

  it("accepts an external URL found in immutable evidence even when claim quote omits it", async () => {
    const config = await topicFixture(); const url = "https://example.com/evidence/42";
    const oldCitation = "^[Companion-2026-09-17-aaaaaaaaaaaa.md:9]";
    const previous = `---\ntitle: 旧发布\nprojectId: companion\n---\n\n旧过程 ${oldCitation}\n`;
    await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "replay.md"), previous);
    const record = revisionRecord("a", `## 发布方案\n\n{{claim:0}}\n\n依据 ${url}`, sha256Text(previous));
    const support = `支持依据（${url}）`;
    record.payload.evidence.push({ id: "e1", kind: "user", text: support, sha256: sha256Text(support),
      originalSha256: sha256Text(support), locator: `knowledge-evidence://a/${sha256Text(support)}`, observedAt: "2026-09-17T00:00:00Z" });
    record.payload.claims[0].supportingQuotes = [{ evidenceId: "e1", quote: support }];
    record.payload.topicRevisions![0].citationRetirements = [{ citation: oldCitation, reason: "外部证据承接", replacement: url }];

    await materializeRecords(config, [record]);

    expect(record.payload.claims[0].quote).not.toContain(url);
    expect(await readFile(path.join(config.wikiRoot, "wiki", "concepts", "replay.md"), "utf8")).toContain(url);
  });

  it("prunes retired source and refs without migration, then keeps a dependent revision basis stable", async () => {
    const seed = await topicFixture();
    const first = revisionRecord("a", "## 发布方案\n\n{{claim:0}}", null);
    await materializeRecords(seed, [first]);
    const firstBody = (await topicFiles(seed)).get("replay.md")!;
    const firstCitation = sourceCitation("a"); const secondCitation = sourceCitation("b");
    expect(firstBody).toContain(firstCitation);
    const second = revisionRecord("b", `## 发布方案\n\n{{claim:0}}\n\n替代引用 ${secondCitation}`,
      sha256Text(firstBody), [first.id], { citation: firstCitation, reason: "移除结束的过程证据", replacement: secondCitation });
    const secondConfig = await topicFixture();
    await writeFile(path.join(secondConfig.wikiRoot, "wiki", "concepts", "replay.md"), firstBody);
    const baselineSources = await topicFiles(seed, "sources");
    await mkdir(path.join(secondConfig.wikiRoot, "sources"), { recursive: true });
    for (const [name, content] of baselineSources) await writeFile(path.join(secondConfig.wikiRoot, "sources", name), content);
    await materializeRecords(secondConfig, [first, second]);
    const secondBody = (await topicFiles(secondConfig)).get("replay.md")!;
    const third = revisionRecord("c", `${parseFrontmatter(secondBody).body}\n\n后续 {{claim:0}}`,
      sha256Text(secondBody), [first.id, second.id]);

    const left = await topicFixture(); const right = await topicFixture();
    await materializeRecords(left, [first, second, third]);
    await materializeRecords(right, [third, second, first]);

    expect(await topicFiles(left)).toEqual(await topicFiles(right));
    expect(await topicFiles(left, "sources")).toEqual(new Map([
      [`Companion-2026-09-17-bbbbbbbbbbbb.md`, expect.any(String)],
      [`Companion-2026-09-17-cccccccccccc.md`, expect.any(String)],
    ]));
    const body = (await topicFiles(left)).get("replay.md")!;
    expect(parseFrontmatter(body).meta.knowledgePublicationRefs).toEqual([`${second.id}:0`, `${third.id}:0`]);
    expect(body).not.toContain(firstCitation);
  });

  it("rejects a guessed URL that is absent from evidence and prior sources", () => {
    const forged = { citation: "^[old.md:1]", reason: "过程已结束", replacement: "https://example.com/forged" };
    expect(() => validateRetirementReferences([forged], ["没有对应的原始证据"])).toThrow(/original evidence/);
  });

  it("rejects a new-evidence URL that quoteContribution would otherwise narrow away", async () => {
    const config = await topicFixture(); const url = "https://example.com/unquoted";
    const previous = `---\ntitle: 旧发布\nprojectId: companion\nknowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试\n---\n\n旧过程 ^[legacy.md:1]\n`;
    await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "replay.md"), previous);
    await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
    await writeFile(path.join(config.wikiRoot, "sources", "legacy.md"), "历史过程，无外部链接。\n");
    const result = await consolidateSession(consolidationJob(url), consolidationConfig(config, url), new Map([[pageId, previous]]));
    expect(result).toMatchObject({ status: "error", retryable: false });
    expect(result.error).toMatch(/original evidence/); expect(result.contribution).toBeUndefined();
  });
});
