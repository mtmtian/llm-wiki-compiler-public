/** User-visible session consolidation: reuse topics, retain evidence, and reject unsafe edits. */
import { describe, expect, it, onTestFinished } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { resolvePlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import type { TopicPlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { sha256Text } from "../src/connectors/hash.js";
import { buildFrontmatter } from "../src/utils/markdown.js";
import type { FlowConfig, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";

const pageId = "concepts/sample-creative-budget";
const topicId = sha256Text("budget");
const original = `${buildFrontmatter({ title: "样例素材测试预算", projectId: "companion", knowledgeTopicId: topicId,
  knowledgeTopic: "样例素材推广", knowledgeDecisionObject: "样例素材测试预算" })}\n\n当前模拟预算 12 个虚构单位。^[old.md:1]\n`;

function job(): FlowJob {
  const text = "样例素材测试预算由12个虚构单位调整为28个虚构单位，其他条件不变。";
  return { id: "session-batch-2", projectId: "companion", projectLabel: "Companion", sessionId: "s", turnId: "t2",
    cwd: "/tmp/project", createdAt: "2025-01-15T00:00:00Z", prompt: text, lastAssistant: "",
    allowedPageIds: [pageId], evidence: [{ id: "user-2", kind: "user", text, sha256: sha256Text(text),
      observedAt: "2025-01-15T00:00:00Z", locator: "codex://s/t2" }],
    sessionContext: { version: 1, revision: 1, summary: "正在讨论样例素材测试预算。", topicPageIds: [pageId], evidence: [] } };
}

function plan(): TopicPlan {
  return { summary: "样例素材测试预算调整为28个虚构单位。", disposition: "edit", reason: "现有预算页可更新",
    pages: [{ action: "update", targetPageId: pageId, topic: "样例素材推广", decisionObject: "样例素材测试预算",
      title: "样例素材测试预算", reason: "同一对象已存在，保留旧预算历史" }] };
}

function draft(): TopicDraft {
  const input = job();
  return { claims: [{ text: input.prompt, evidenceId: "user-2", quote: input.prompt, title: "预算调整",
    topic: "样例素材推广", decisionObject: "样例素材测试预算", slug: "sample-material-budget", targetPageId: pageId,
    kind: "decision", status: "decided", useWhen: "执行样例素材测试预算调整时", rationale: "用户明确调整样例预算",
    replacementIntent: false, supportingQuotes: [] }], pages: [{ pageId,
    body: "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n此前预算 12 个虚构单位。^[old.md:1]",
    claimIndexes: [0] }], summary: "保留用户确认的样例预算调整与旧预算历史。" };
}

function config(responses: Record<string, unknown>, onSystem?: (system: string, toolName: string) => void): FlowConfig {
  const stateDir = mkdtempSync(path.join(tmpdir(), "consolidation-state-"));
  mkdirSync(path.join(stateDir, "wiki/sources"), { recursive: true });
  writeFileSync(path.join(stateDir, "wiki/sources/old.md"), "用户确认样例预算为12个虚构单位，适用样例素材测试。\n");
  onTestFinished(() => rm(stateDir, { recursive: true, force: true }));
  const unsupported = async (): Promise<never> => { throw new Error("unexpected provider operation"); };
  const provider: LLMProvider = { complete: unsupported, stream: unsupported, embed: unsupported,
    toolCall: async (_system, messages, tools) => {
      onSystem?.(_system, tools[0].name);
      if (!(tools[0].name in responses)) throw new Error(`unplanned tool ${tools[0].name}`);
      const response = responses[tools[0].name];
      const request = JSON.parse(messages[0].content) as Record<string, any>;
      const value = typeof response === "function" ? response(request) : response;
      return JSON.stringify(toCorrectionShape(tools[0].name, request, value));
    } };
  return { wikiRoot: path.join(stateDir, "wiki"), stateDir, model: "test", maxProposals: 5,
    maxPendingPerProject: 10, provider, reviewer: provider, machineId: "test",
    exchange: { root: "/tmp/exchange", protocolVersion: 2, participants: ["test"] } };
}

function toCorrectionShape(toolName: string, request: Record<string, any>, value: unknown): unknown {
  if (toolName === "knowledge_topic_plan" && request.correction) return { plan: value };
  if (toolName !== "knowledge_topic_edit" || !request.correction || !value || typeof value !== "object") return value;
  const options = (request.evidence ?? []).flatMap((item: any) => item.quoteOptions ?? []);
  const quoteId = (quote: string) => options.find((item: any) => item.quote === quote)?.quoteId ?? "unknown-quote";
  const draft = value as Record<string, any>;
  return { ...draft, claims: (draft.claims ?? []).map((claim: Record<string, any>) => {
    const { quote, evidenceId: _evidenceId, topic: _topic, decisionObject: _decisionObject, supportingQuotes, ...fields } = claim;
    return { ...fields, quoteId: claim.quoteId ?? quoteId(quote),
      supportingQuotes: (supportingQuotes ?? []).map((item: Record<string, any>) => ({
        quoteId: item.quoteId ?? quoteId(item.quote),
      })) };
  }) };
}

function accepted() {
  return { decision: "accept", reason: "保留旧预算历史，原文明确支持新预算与同一对象", checkedClaimIndexes: [0], checkedPageIds: [pageId] };
}

/** Preserve the publication assertion while sharing fixture invocation between correction cases. */
async function expectSubmittedSession(runtime: FlowConfig) {
  const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  return result;
}

async function expectHeldDraft(changed: TopicDraft, reason: RegExp, input = job()): Promise<void> {
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed, knowledge_topic_review: accepted() });
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  expect(result).toMatchObject({ status: "needs_review", error: expect.stringMatching(reason) });
  expect(result.contribution).toBeUndefined();
}

async function submitDraft(changed: TopicDraft, review: ReturnType<typeof accepted> & { checkedRetiredCitations?: string[] } = accepted()) {
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed, knowledge_topic_review: review });
  return consolidateSession(job(), runtime, new Map([[pageId, original]]));
}

async function submitDraftWithPlan(topicPlan: ReturnType<typeof plan>, changed: TopicDraft,
  review: ReturnType<typeof accepted> & { checkedRetiredCitations?: string[] }) {
  const runtime = config({ knowledge_topic_plan: topicPlan, knowledge_topic_edit: changed, knowledge_topic_review: review });
  return consolidateSession(job(), runtime, new Map([[pageId, original]]));
}

describe("whole-session topic consolidation", () => {
  it("Given retired session destinations, When the next increment is planned, Then only the current catalog can guide it", async () => {
    const input = job(); input.sessionContext!.topicPageIds.push("concepts/retired-fragment");
    const runtime = config({ knowledge_topic_plan: (request: { sessionContext: { topicPageIds: string[] } }) => {
      expect(request.sessionContext.topicPageIds).toEqual([pageId]); return plan();
    }, knowledge_topic_edit: draft(), knowledge_topic_review: accepted() });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(result.sessionMemory?.topicPageIds).toEqual([pageId]);
  });

  it("Given an existing decision, When the user changes it, Then updates one page and preserves history", async () => {
    const result = await submitDraft(draft());
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions).toMatchObject([{ pageId, topicId, basisHash: sha256Text(original) }]);
    expect(result.contribution?.topicRevisions?.[0].body).toContain("此前预算 12 个虚构单位。^[old.md:1]");
    expect(result.sessionMemory?.topicPageIds).toEqual([pageId]);
    expect(result.contribution?.evidence[0].text).toBe(job().prompt);
  });

  it("Given stale labels on an existing page, When an update plan selects that page with corrected labels, Then identity and basis stay stable while labels change", () => {
    const corrected = { ...plan(), pages: [{ ...plan().pages[0], title: "样例素材测试预算", topic: "样例素材测试", decisionObject: "样例第二轮素材调整" }] };
    const resolved = resolvePlan(corrected, job(), new Map([[pageId, original]]));
    expect(resolved[0]).toMatchObject({ pageId, topicId, basisHash: sha256Text(original), title: "样例素材测试预算",
      topic: "样例素材测试", decisionObject: "样例第二轮素材调整" });
  });

  it("Given a coherent same-page label correction, When review accepts the full edit, Then the revision carries new labels and preserves the old rationale", async () => {
    const correctedPlan = { ...plan(), pages: [{ ...plan().pages[0], title: "样例素材测试预算", topic: "样例素材测试", decisionObject: "样例第二轮素材调整" }] };
    const changed = draft(); changed.claims[0].topic = "样例素材测试"; changed.claims[0].decisionObject = "样例第二轮素材调整";
    const result = await submitDraftWithPlan(correctedPlan, changed, accepted());
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions?.[0]).toMatchObject({ pageId, topicId,
      topic: "样例素材测试", decisionObject: "样例第二轮素材调整", basisHash: sha256Text(original) });
    expect(result.contribution?.topicRevisions?.[0].body).toContain("此前预算 12 个虚构单位。^[old.md:1]");
  });

  it("Given a corrected label that changes the business object, When independent review holds it, Then the same page is not published", async () => {
    const correctedPlan = { ...plan(), pages: [{ ...plan().pages[0], topic: "完全无关的主题", decisionObject: "另一项业务决策" }] };
    const changed = draft(); changed.claims[0].topic = "完全无关的主题"; changed.claims[0].decisionObject = "另一项业务决策";
    const result = await submitDraftWithPlan(correctedPlan, changed,
      { ...accepted(), decision: "needs_review", reason: "独立审阅认为新标签与原决策对象不一致" });
    expect(result).toMatchObject({ status: "needs_review", error: "独立审阅认为新标签与原决策对象不一致" });
    expect(result.contribution).toBeUndefined();
  });

  it("Given no durable change, When planning says noop, Then returns only local session context", async () => {
    const runtime = config({ knowledge_topic_plan: { ...plan(), disposition: "noop", pages: [] } });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("empty");
    expect(result.contribution).toBeUndefined();
    expect(result.sessionMemory?.topicPageIds).toEqual([pageId]);
  });

  it("Given an ambiguous change, When independent review holds it, Then publishes nothing", async () => {
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(),
      knowledge_topic_review: { ...accepted(), decision: "needs_review", reason: "预算单位不明确" } });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("needs_review");
    expect(result.contribution).toBeUndefined();
    expect(result.error).toContain("预算单位不明确");
  });

  it("Given a same-name topic in another project, Then cannot route a revision into it", async () => {
    const runtime = config({ knowledge_topic_plan: plan() });
    const result = await consolidateSession(job(), runtime,
      new Map([[pageId, original.replace("projectId: companion", "projectId: other")]]));
    expect(result).toMatchObject({ status: "needs_review", error: expect.stringMatching(/project/) });
    expect(result.contribution).toBeUndefined();
  });

  it("Given a cached acceptance missing page review, Then holds it durably instead of retrying the same failure", async () => {
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(),
      knowledge_topic_review: { ...accepted(), checkedPageIds: [] } });
    const first = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(first).toMatchObject({ status: "needs_review", error: expect.stringMatching(/omitted/) });
    expect(first.contribution).toBeUndefined();
    const unavailable = config({}).provider;
    expect(await consolidateSession(job(), { ...runtime, provider: unavailable, reviewer: unavailable }, new Map([[pageId, original]]))).toEqual(first);
  });

  it("Given historical evidence outside the new batch, Then supplies original source context for full-page review", async () => {
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(),
      knowledge_topic_review: (request: { priorSources: Record<string, string> }) => {
        expect(request.priorSources["old.md"]).toContain("用户确认样例预算为12个虚构单位"); return accepted();
      } });
    expect((await consolidateSession(job(), runtime, new Map([[pageId, original]]))).status).toBe("submitted");
  });

  it.each([
    { scope: "one-time execution", required: ["one-time execution log", "surviving same-page citation or claim",
      "do not require repeating those transient details or inventing an external process record"] },
    { scope: "durable context", required: ["unique rationale, budget, constraint, risk, counterexample",
      "Reject guessed closure", "treating an old date as expiry"] },
    { scope: "decision validity", required: ["observedAt is capture time", "updatedAt is page publication time",
      "approval is not proof of completion", "A newer observation alone does not supersede an existing decision"] },
  ])("Given $scope retirement, Then the independent review retains the required policy", async ({ required }) => {
    let reviewPrompt = "";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(), knowledge_topic_review: accepted() }, (system, toolName) => {
      if (toolName === "knowledge_topic_review") reviewPrompt = system;
    });
    expect((await consolidateSession(job(), runtime, new Map([[pageId, original]]))).status).toBe("submitted");
    for (const clause of required) expect(reviewPrompt).toContain(clause);
  });

  it("Given a draft deleting historical citations, Then rejects it before publication", async () => {
    const changed = draft(); changed.pages[0].body = "## 当前结论\n预算28个虚构单位。{{claim:0}}";
    await expectHeldDraft(changed, /citation/);
  });

  it("Given an explicit evidence retirement, Then acceptance requires independent review of that retirement", async () => {
    const changed = draft(); changed.pages[0].body = "## 当前结论\n预算调整及条件 {{claim:0}}";
    changed.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "新决定引用已完整承接原金额与调整条件。", replacement: "{{claim:0}}" }];
    const result = await submitDraft(changed, { ...accepted(), checkedRetiredCitations: ["^[old.md:1]"] });
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions?.[0].citationRetirements).toEqual(changed.pages[0].citationRetirements);
    expect(result.contribution?.topicRevisions?.[0].body).not.toContain("^[old.md:1]");
  });

  it("Given an otherwise accepted edit without retirement review coverage, Then it stays unpublished", async () => {
    const changed = draft(); changed.pages[0].body = "## 当前结论\n预算调整及条件 {{claim:0}}";
    changed.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "新决定承接原条件。", replacement: "{{claim:0}}" }];
    await expectHeldDraft(changed, /review omitted an evidence retirement/);
  });

  it("Given a model invents a GitHub process reference, Then a retirement is held before publishing", async () => {
    const changed = draft(); const invented = "https://github.com/example/wiki/pull/99";
    changed.pages[0].body = `## 当前结论\n预算调整及条件 {{claim:0}}\n\n过程见 ${invented}`;
    changed.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧记录移交外部。", replacement: invented }];
    await expectHeldDraft(changed, /not supported by original evidence/);
  });

  it("Given a hallucinated exact quote, Then cannot publish even if a reviewer would approve", async () => {
    const changed = draft(); changed.claims[0].quote = "伪造预算280个虚构单位";
    await expectHeldDraft(changed, /evidence/);
  });

  it("Given a saved model result, When the process resumes without a provider, Then recovers the same contribution", async () => {
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(), knowledge_topic_review: accepted() });
    const existing = new Map([[pageId, original]]);
    const first = await consolidateSession(job(), runtime, existing);
    const unavailable = config({}).provider;
    const second = await consolidateSession(job(), { ...runtime, provider: unavailable, reviewer: unavailable }, existing);
    expect(second).toEqual(first);
  });

  it("Given an approval referring to a prior proposal, Then preserves both original quotes in the publication", async () => {
    const input = job();
    input.evidence = [{ ...input.evidence[0], text: "可以，按这个预算执行", sha256: sha256Text("可以，按这个预算执行") }];
    input.sessionContext!.evidence = [{ id: "proposal", kind: "assistant", text: "建议将样例预算调整到28个虚构单位。",
      locator: "codex://s/t1", observedAt: "2026-09-17T00:00:00Z", sha256: sha256Text("建议将样例预算调整到28个虚构单位。") }];
    const changed = draft(); changed.claims[0].quote = input.evidence[0].text;
    changed.claims[0].supportingQuotes = [{ evidenceId: "proposal", quote: "建议将样例预算调整到28个虚构单位。" }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed, knowledge_topic_review: accepted() });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.contribution?.evidence.map(item => [item.kind, item.text])).toEqual([
      ["user", "可以，按这个预算执行"], ["assistant", "建议将样例预算调整到28个虚构单位。"]]);
    expect(result.contribution?.claims[0].supportingQuotes).toEqual([{ evidenceId: "quote-1", quote: "建议将样例预算调整到28个虚构单位。" }]);
  });

  it("Given a session summary alone, Then cannot use it in place of the missing original proposal", async () => {
    const changed = draft();
    changed.claims[0].supportingQuotes = [{ evidenceId: "summary", quote: "正在讨论样例素材测试预算。" }];
    await expectHeldDraft(changed, /evidence/);
  });

  it("Given a saved specification labeled as a decision, Then it becomes a historical fact that still requires independent review", async () => {
    const input = job(); input.evidence[0].kind = "artifact";
    const changed = draft(); changed.claims[0].status = "historical";
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed,
      knowledge_topic_review: () => { reviews += 1; return accepted(); } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted"); expect(reviews).toBe(1);
    expect(result.contribution?.claims[0]).toMatchObject({ kind: "fact", status: "historical" });
  });

  it("Given a correctable draft error, When corrected using the same original evidence, Then still requires whole-page review", async () => {
    const invalid = draft(); invalid.claims[0].evidenceId = "invented-summary";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown; evidence?: Array<{ quoteOptions: Array<{ quoteId: string }> }> }) => request.correction
      ? { ...draft(), claims: [{ ...draft().claims[0], quote: undefined, quoteId: request.evidence![0].quoteOptions[0].quoteId }] }
      : invalid,
      knowledge_topic_review: accepted() });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(result.contribution?.evidence.map(item => item.text)).toEqual([job().prompt]);
    expect(result.contribution?.claims[0].evidenceId).toBe("quote-0");
  });

  it("Given an invalid retirement literal in the first draft, When one correction supplies a single survivor, Then the existing correction path publishes it", async () => {
    const invalid = draft(); invalid.pages[0].body = "## 当前结论\n预算调整 {{claim:0}}";
    invalid.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧过程结束",
      replacement: "{{claim:0}}、{{claim:2}}" }];
    const corrected = structuredClone(invalid);
    corrected.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧过程结束", replacement: "{{claim:0}}" }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown }) => request.correction ? corrected : invalid,
      knowledge_topic_review: { ...accepted(), checkedRetiredCitations: ["^[old.md:1]"] } });
    const result = await expectSubmittedSession(runtime);
    expect(result.contribution?.topicRevisions?.[0].citationRetirements?.[0].replacement).toBe("{{claim:0}}");
  });

  it("Given an invalid retirement literal in both drafts, When correction repeats it, Then the bounded run holds with the concrete schema failure", async () => {
    const invalid = draft(); invalid.pages[0].body = "## 当前结论\n预算调整 {{claim:0}}";
    invalid.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧过程结束",
      replacement: "{{claim:0}}、{{claim:2}}" }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown }) => request.correction ? invalid : invalid,
      knowledge_topic_review: accepted() });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result).toMatchObject({ status: "needs_review", error: expect.stringMatching(/correction evidence selection failed|pattern/) });
    expect(result.contribution).toBeUndefined();
  });

  it("Given a correction with a dropped citation, Then it receives the exact unaccounted marker by page", async () => {
    const input = job(); input.evidence[0].kind = "artifact";
    const invalid = draft(); invalid.claims[0].evidenceId = "missing-evidence";
    invalid.pages[0].body = "## 当前结论\n预算28个虚构单位。{{claim:0}}";
    const corrected = draft(); corrected.claims[0].kind = "fact"; corrected.claims[0].status = "historical";
    let correction: any;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
      if (!request.correction) return invalid;
      correction = request.correction;
      return corrected;
    }, knowledge_topic_review: accepted() });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(correction.unaccountedCitations).toEqual([{ pageId, citations: ["^[old.md:1]"] }]);
  });

  it.each(["assistant", "artifact"] as const)("Given %s primary and assistant support, When authority normalization drops unsupported context, Then no correction is spent and independent review still runs", async (primaryKind) => {
    const input = job(); const primaryText = `${primaryKind} historical evidence`; const supportText = "assistant proposal context";
    input.evidence = [
      { ...input.evidence[0], id: `${primaryKind}-primary`, kind: primaryKind, text: primaryText, sha256: sha256Text(primaryText) },
      { ...input.evidence[0], id: "assistant-support", kind: "assistant", text: supportText, sha256: sha256Text(supportText) },
    ];
    const invalid = draft(); invalid.claims[0] = { ...invalid.claims[0], text: primaryText, evidenceId: `${primaryKind}-primary`, quote: primaryText,
      kind: primaryKind === "assistant" ? "lesson" : "fact", status: "historical", supportingQuotes: [{ evidenceId: "assistant-support", quote: supportText }] };
    let corrections = 0; let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
      if (!request.correction) return invalid;
      corrections += 1;
      const option = request.evidence.find((item: any) => item.id === `${primaryKind}-primary`).quoteOptions[0];
      return { ...invalid, claims: [{ ...invalid.claims[0], quote: undefined, quoteId: option.quoteId, supportingQuotes: [] }] };
    }, knowledge_topic_review: () => { reviews += 1; return accepted(); } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted"); expect(corrections).toBe(0); expect(reviews).toBe(1);
    expect(result.contribution?.claims[0].kind).toBe(primaryKind === "assistant" ? "lesson" : "fact");
    expect(result.contribution?.claims[0].supportingQuotes ?? []).toEqual([]);
  });

  it("Given a malformed non-edit plan, When correction keeps the same destination, Then the full review still runs", async () => {
    const invalid = { ...plan(), disposition: "needs_review" as const };
    const runtime = config({ knowledge_topic_plan: (request: { correction?: unknown }) => request.correction ? plan() : invalid,
      knowledge_topic_edit: draft(), knowledge_topic_review: accepted() });
    const result = await expectSubmittedSession(runtime);
    expect(result.contribution?.topicRevisions?.map(page => page.pageId)).toEqual([pageId]);
  });

  it("does not spend a plan correction on a missing cited source", async () => {
    const runtime = config({ knowledge_topic_plan: (request: { correction?: unknown }) => {
      expect(request.correction).toBeUndefined(); return plan();
    } });
    await rm(path.join(runtime.wikiRoot, "sources/old.md"));
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("needs_review");
    expect(result.error).toMatch(/ENOENT|source/);
  });

  it("Given a new session about the same decision object, Then reuses the project page without prior session associations", async () => {
    const input = { ...job(), id: "another-session", sessionId: "other", sessionContext: undefined };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(), knowledge_topic_review: accepted() });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.contribution?.topicRevisions?.map(page => page.pageId)).toEqual([pageId]);
  });
});
