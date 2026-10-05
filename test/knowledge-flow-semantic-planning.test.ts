/** Acceptance tests for shared semantic topic planning and bounded revision review. */
import { describe, expect, it, onTestFinished } from "vitest";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { validatedDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { MAX_TOPIC_BODY_CHARS, resolvePlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import type { TopicPlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowConfig, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";
import { buildFrontmatter, parseFrontmatter } from "../src/utils/markdown.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { quoteBoundDraftFromCatalog } from "./knowledge-flow-consolidation-fixtures.js";

const sharedPage = "concepts/shared-decision";
const peerPage = "concepts/peer-guidance";
const sharedOriginal = `${buildFrontmatter({ title: "共享决策", projectId: "alpha",
  knowledgeTopicId: "stable-topic-id", knowledgeTopic: "知识组织", knowledgeDecisionObject: "共享知识主题" })}\n\n旧结论。\n`;
const peerOriginal = `${buildFrontmatter({ title: "来源说明", projectId: "gamma",
  knowledgeTopicId: "peer-topic-id", knowledgeTopic: "知识组织", knowledgeDecisionObject: "其他说明" })}\n\n仅供候选判断。\n`;
const semanticOriginal = `${buildFrontmatter({ title: "跨项目主题", topicScope: "semantic", sourceProjectIds: ["alpha", "beta"],
  knowledgeTopicId: "semantic-topic-id", knowledgeTopic: "知识组织", knowledgeDecisionObject: "共享知识主题" })}\n\n共享原文。\n`;

function job(projectId = "beta", topicScope?: "semantic"): FlowJob {
  const text = "把 wiki 按语义主题组织，同一主题保留各来源项目的适用条件。";
  return { id: `semantic-${projectId}`, projectId, projectLabel: projectId.toUpperCase(), sessionId: "session",
    turnId: "turn", cwd: "/tmp/project", createdAt: "2026-09-26T00:00:00Z", prompt: text, lastAssistant: "",
    evidence: [{ id: "user-evidence", kind: "user", text, sha256: sha256Text(text), observedAt: "2026-09-26T00:00:00Z", locator: "codex://session/turn" }],
    ...(topicScope ? { topicScope } : {}), allowedPageIds: [sharedPage, peerPage],
    sessionContext: { version: 1, revision: 1, summary: "跨项目整理 wiki 主题。", topicPageIds: [], evidence: [] } };
}

function plan(target = sharedPage): TopicPlan {
  return { summary: "按共同知识主题复用页面。", disposition: "edit", reason: "同一决策对象",
    pages: [{ action: "update", targetPageId: target, title: "共享决策", topic: "知识组织",
      decisionObject: "共享知识主题", reason: "同一语义对象" }] };
}

function draft(input: FlowJob, target = sharedPage): TopicDraft {
  const evidence = input.evidence[0];
  return { summary: "按语义主题组织，保留来源项目适用范围。", claims: [{ text: evidence.text,
    evidenceId: evidence.id, quote: evidence.text, title: "知识主题组织", topic: "知识组织",
    decisionObject: "共享知识主题", slug: "semantic-topics", targetPageId: target, kind: "decision",
    status: "decided", useWhen: "整理跨项目知识", rationale: "用户明确要求按语义组织",
    replacementIntent: false, supportingQuotes: [] }],
    pages: [{ pageId: target, body: `## 当前结论\n按语义主题组织，并保留来源项目适用范围。{{claim:0}}`, claimIndexes: [0] }] };
}

function revisionJob(scope?: "semantic"): FlowJob {
  const input = job("beta", scope);
  input.allowedPageIds = [sharedPage];
  return input;
}

function config(root = mkdtempSync(path.join(tmpdir(), "semantic-planning-"))): FlowConfig {
  onTestFinished(() => rm(root, { recursive: true, force: true }));
  mkdirSync(path.join(root, "wiki", "concepts"), { recursive: true });
  writeFileSync(path.join(root, "wiki", `${sharedPage}.md`), sharedOriginal);
  writeFileSync(path.join(root, "wiki", `${peerPage}.md`), peerOriginal);
  return { wikiRoot: root, stateDir: path.join(root, "state"), model: "test",
    maxProposals: 5, maxPendingPerProject: 10, topicScope: "semantic",
    exchange: { root: path.join(root, "exchange"), protocolVersion: 2, participants: ["test"] } };
}

function fakeProvider(input: FlowJob, captured: Record<string, any>[]): LLMProvider {
  const unsupported = async (): Promise<never> => { throw new Error("unexpected provider operation"); };
  return { complete: unsupported, stream: unsupported, embed: unsupported, toolCall: async (system, messages, tools) => {
    const request = JSON.parse(messages[0].content) as Record<string, any>;
    captured.push({ tool: tools[0].name, system, request });
    if (tools[0].name === "knowledge_topic_plan") return JSON.stringify(plan());
    if (tools[0].name === "knowledge_topic_edit") return JSON.stringify(
      quoteBoundDraftFromCatalog(draft(input), buildCorrectionEvidence(input.evidence)));
    if (tools[0].name === "knowledge_topic_review") return JSON.stringify({ decision: "accept", reason: "来源范围与证据完整",
      checkedClaimIndexes: [0], checkedPageIds: [sharedPage] });
    throw new Error(`unexpected tool ${tools[0].name}`);
  } };
}

function wikiPage(pageId: string, index: number, bodyLength: number, summaryLength = 0): { pageId: string; text: string } {
  const meta = { title: `主题页 ${index}`, projectId: "alpha", knowledgeTopicId: `topic-${index}`,
    knowledgeTopic: index === 0 ? "知识组织" : `候选主题 ${index}`,
    knowledgeDecisionObject: index === 0 ? "共享知识主题" : `候选对象 ${index}`,
    ...(summaryLength ? { summary: "s".repeat(summaryLength) } : {}) };
  return { pageId, text: `${buildFrontmatter(meta)}\n\n## 内容\n${"x".repeat(bodyLength)}\n` };
}

function seedPages(runtime: FlowConfig, input: FlowJob, pages: Array<{ pageId: string; text: string }>): void {
  input.allowedPageIds = pages.map(page => page.pageId);
  for (const page of pages) writeFileSync(path.join(runtime.wikiRoot, "wiki", `${page.pageId}.md`), page.text);
}

function concurrentCreation(projectId: string, title: string): PublicationRecord {
  const input = job(projectId, "semantic");
  const proposed: TopicPlan = { ...plan(), pages: [{ ...plan().pages[0], action: "create", targetPageId: null, title }] };
  const pages = resolvePlan(proposed, input, new Map());
  const validated = validatedDraft(draft(input, pages[0].pageId), input, pages, 5);
  return { id: sha256Text(projectId), payload: { version: 2, baselineId: "b".repeat(64),
    machineId: projectId, projectId, projectLabel: input.projectLabel, createdAt: input.createdAt,
    originJobHash: sha256Text(input.id), repoIdentity: null, basisRecordIds: [],
    review: { status: "accepted", model: "test" }, claims: validated.claims,
    evidence: input.evidence, topicRevisions: validated.revisions } };
}

describe("semantic topic planning", () => {
  it("Given a shared page owned by another project, When a semantic job updates it, Then preserves page identity and basis", () => {
    const input = revisionJob("semantic");
    const pages = resolvePlan(plan(), input, new Map([[sharedPage, sharedOriginal]]));
    expect(pages[0]).toMatchObject({ pageId: sharedPage, topicId: "stable-topic-id", basisHash: sha256Text(sharedOriginal) });
  });

  it("Given equivalent new topics from different projects, When plans create them, Then topic and page identities match without project labels", () => {
    const semanticPlan = { ...plan(), pages: [{ ...plan().pages[0], action: "create" as const,
      targetPageId: null, title: "共享决策" }] };
    const first = resolvePlan(semanticPlan, revisionJob("semantic"), new Map());
    const secondJob = { ...revisionJob("semantic"), projectId: "gamma", projectLabel: "Gamma" };
    const second = resolvePlan(semanticPlan, secondJob, new Map());
    expect(first[0].topicId).toBe(second[0].topicId);
    expect(first[0].pageId).toBe(second[0].pageId);
    expect(first[0].pageId).not.toContain("BETA");
    expect(first[0].pageId).toBe("concepts/知识组织-" + first[0].topicId.slice(0, 8));
  });

  it("Given concurrent same-topic creations with different titles, When materialized, Then both are held instead of creating duplicate pages", async () => {
    const runtime = config();
    const records = [concurrentCreation("beta", "语义主题组织"), concurrentCreation("gamma", "按知识主题整理")];
    const result = await materializeRecords(runtime, records);
    expect(result.conflicts).toHaveLength(2);
    expect(result.conflicts.every(conflict => conflict.reason.includes("concurrent"))).toBe(true);
    expect((await readdir(path.join(runtime.wikiRoot, "wiki", "concepts"))).sort())
      .toEqual(["peer-guidance.md", "shared-decision.md"]);
  });

  it("Given a legacy project job, When its plan selects another project's page, Then rejects the route", () => {
    expect(() => resolvePlan(plan(), revisionJob(), new Map([[sharedPage, sharedOriginal]])))
      .toThrow(/project scope|ownership/);
  });

  it("Given a shared semantic page with no project owner, When an old job targets it, Then rejects the update", () => {
    expect(() => resolvePlan(plan(), revisionJob(), new Map([[sharedPage, semanticOriginal]])))
      .toThrow(/legacy job cannot update semantic topic/);
  });

  it("Given a valid semantic draft, When validated, Then marks only its revision semantic", () => {
    const semanticJob = revisionJob("semantic");
    const page = resolvePlan(plan(), semanticJob, new Map([[sharedPage, sharedOriginal]]));
    const result = validatedDraft(draft(semanticJob), semanticJob, page, 5);
    expect(result.revisions[0].topicScope).toBe("semantic");
    const legacyJob = revisionJob();
    const legacyPage = resolvePlan(plan(), legacyJob, new Map([[sharedPage, sharedOriginal.replace("projectId: alpha", "projectId: beta")]]));
    expect(validatedDraft(draft(legacyJob), legacyJob, legacyPage, 5).revisions[0].topicScope).toBeUndefined();
  });

  it.each([
    ["missing semantic config", { topicScope: undefined }],
    ["missing session context", { session: false }],
    ["legacy exchange protocol", { protocolVersion: 1 }],
    ["disabled consolidation", { disabled: true }],
    ["submitted claims", { submittedClaims: true }],
  ])("Given a semantic job with %s, When processJob validates it, Then rejects before provider calls", async (_label, change) => {
    const input = revisionJob("semantic");
    const runtime = config();
    const called: string[] = [];
    runtime.provider = { ...fakeProvider(input, []), toolCall: async () => { called.push("provider"); return "{}"; } };
    if ("topicScope" in change) runtime.topicScope = undefined;
    if ("session" in change && change.session === false) input.sessionContext = undefined;
    if ("protocolVersion" in change) runtime.exchange!.protocolVersion = change.protocolVersion;
    if ("disabled" in change) runtime.sessionConsolidation = { enabled: false };
    if ("submittedClaims" in change) input.submittedClaims = [];
    const result = await processJob(input, runtime);
    expect(result).toMatchObject({ status: "error", error: expect.stringMatching(/semantic topic scope/) });
    expect(called).toEqual([]);
  });

  it("Given an unknown job or config scope, When processJob validates it, Then rejects before provider calls", async () => {
    const runtime = config();
    const input = revisionJob();
    (input as unknown as Record<string, unknown>).topicScope = "global";
    expect(await processJob(input, runtime)).toMatchObject({ status: "error", error: "invalid job topic scope" });
    (input as unknown as Record<string, unknown>).topicScope = undefined;
    (runtime as unknown as Record<string, unknown>).topicScope = "global";
    expect(await processJob(input, runtime)).toMatchObject({ status: "error", error: "invalid configured topic scope" });
  });

  it("Given semantic pages whose total bodies exceed 120000 characters, When compact catalog and selected text fit, Then processJob succeeds", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const pages = Array.from({ length: 13 }, (_, index) => wikiPage(index ? `concepts/large-${index}` : sharedPage, index, 11_200));
    seedPages(runtime, input, pages);
    expect(pages.reduce((total, page) => total + page.text.length, 0)).toBeGreaterThan(120_000);
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    runtime.reviewer = runtime.provider;
    const result = await processJob(input, runtime);
    expect(result.status).toBe("submitted");
    expect(captured.map(call => call.tool)).toEqual(["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review"]);
  });

  it("Given a scoped page whose generated frontmatter exceeds the page budget, When a semantic job runs, Then it still plans and submits", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const page = wikiPage(sharedPage, 0, 100, 12_500);
    seedPages(runtime, input, [page]);
    expect(page.text.length).toBeGreaterThan(12_000);
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    runtime.reviewer = runtime.provider;
    expect((await processJob(input, runtime)).status).toBe("submitted");
    expect(captured[0].tool).toBe("knowledge_topic_plan");
  });

  it("Given a scoped page whose body exceeds the page budget, When a semantic job runs, Then it is rejected before planning", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    seedPages(runtime, input, [wikiPage(sharedPage, 0, 12_100)]);
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    expect(await processJob(input, runtime)).toMatchObject({ status: "error", error: "scoped page exceeds review budget" });
    expect(captured).toEqual([]);
  });

  it("Given a semantic catalog above budget, When processJob reads the wiki, Then rejects before planning", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const pages = Array.from({ length: 13 }, (_, index) => wikiPage(`concepts/catalog-${index}`, index, 10, 10_300));
    seedPages(runtime, input, pages);
    expect(pages.every(page => page.text.length < 12_000)).toBe(true);
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    const result = await processJob(input, runtime);
    expect(result).toMatchObject({ status: "error", error: expect.stringMatching(/semantic topic context exceeds 120000/) });
    expect(captured).toEqual([]);
  });

  it("Given five selected originals plus a fitting catalog exceed budget, When planning finishes, Then rejects before editing", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const summaries = Array.from({ length: 8 }, (_, index) => wikiPage(`concepts/summary-${index}`, index + 10, 20, 8_800));
    const selected = Array.from({ length: 5 }, (_, index) => wikiPage(`concepts/selected-${index}`, index + 30, 11_200));
    const pages = [...summaries, ...selected];
    seedPages(runtime, input, pages);
    const selectedPlan: TopicPlan = { summary: "更新多个来源页面。", disposition: "edit", reason: "验收预算边界",
      pages: selected.map((page, index) => ({ action: "update", targetPageId: page.pageId,
        title: `主题页 ${index + 30}`, topic: `候选主题 ${index + 30}`, decisionObject: `候选对象 ${index + 30}`, reason: "同一语义范围" })) };
    const calls: string[] = [];
    const unsupported = async (): Promise<never> => { throw new Error("semantic budget should stop before edit"); };
    const provider: LLMProvider = { complete: unsupported, stream: unsupported, embed: unsupported, toolCall: async (_system, _messages, tools) => {
      calls.push(tools[0].name);
      return JSON.stringify(selectedPlan);
    } };
    runtime.provider = provider;
    runtime.reviewer = provider;
    const result = await processJob(input, runtime);
    expect(result).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(/semantic topic context exceeds 120000/) });
    expect(calls).toEqual(["knowledge_topic_plan"]);
  });

  it("Given a semantic cross-project update, When the real provider path runs, Then prompts preserve source scope and review stays bounded", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    runtime.reviewer = runtime.provider;
    const result = await consolidateSession(input, runtime, new Map([[sharedPage, sharedOriginal], [peerPage, peerOriginal]]));
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions?.[0]).toMatchObject({ pageId: sharedPage, topicScope: "semantic" });
    for (const tool of ["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review"]) {
      const call = captured.find(item => item.tool === tool)!;
      expect(call.request.topicScope).toBe("semantic");
      expect(call.request.sourceProjectId).toBe("beta");
      expect(call.system).toContain("same meaning");
      expect(call.system).not.toContain("same project AND decision object");
    }
    const review = captured.find(item => item.tool === "knowledge_topic_review")!.request;
    expect(review.existing.map((item: [string, string]) => item[0])).toEqual([sharedPage]);
    expect(review.existing[0][1]).toBe(sharedOriginal);
    expect(review.pages[0]).not.toHaveProperty("original");
    expect(review.catalog).toHaveLength(2);
    expect(review.catalog[0].sourceProjectIds).toContain("alpha");
    expect(captured.find(item => item.tool === "knowledge_topic_plan")!.system).toContain("applicability");
  });

  it("Given workstream pages, When planning and review run, Then both see each page's body size against the editable limit", async () => {
    const input = revisionJob("semantic");
    const runtime = config();
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    runtime.reviewer = runtime.provider;
    await consolidateSession(input, runtime, new Map([[sharedPage, sharedOriginal], [peerPage, peerOriginal]]));
    const planning = captured.find(item => item.tool === "knowledge_topic_plan")!;
    const review = captured.find(item => item.tool === "knowledge_topic_review")!;
    expect(planning.request.catalog.map((page: any) => page.bodyChars)).toEqual([sharedOriginal, peerOriginal]
      .map(text => parseFrontmatter(text).body.length));
    expect(review.request.catalog[0].bodyChars).toBe(parseFrontmatter(sharedOriginal).body.length);
    expect(planning.system).toContain("Organize pages by workstream");
    expect(planning.system).toContain(`near ${MAX_TOPIC_BODY_CHARS}`);
    expect(review.system).toContain(`near ${MAX_TOPIC_BODY_CHARS}`);
  });

  it("Given a semantic job, When a host supplies legacy extraction dependencies, Then refuses that alternate pipeline", async () => {
    let invoked = false;
    const legacy = async () => { invoked = true; return []; };
    const result = await processJob(revisionJob("semantic"), config(), { extract: legacy, review: legacy });
    expect(result).toMatchObject({ status: "error", error: expect.stringContaining("semantic jobs require the session pipeline") });
    expect(invoked).toBe(false);
  });

  it("Given a missing scope on a legacy job, When topic stages run, Then all stages keep same-project routing", async () => {
    const input = revisionJob();
    const original = sharedOriginal.replace("projectId: alpha", "projectId: beta");
    const runtime = config();
    const captured: Record<string, any>[] = [];
    runtime.provider = fakeProvider(input, captured);
    runtime.reviewer = runtime.provider;
    expect((await consolidateSession(input, runtime, new Map([[sharedPage, original]]))).status).toBe("submitted");
    for (const call of captured) {
      expect(call.request.topicScope).toBeUndefined();
      expect(call.system).toContain("Legacy project scope");
      expect(call.system).not.toContain("Semantic topic scope");
    }
  });
});
