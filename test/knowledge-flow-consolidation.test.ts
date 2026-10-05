/** User-visible session consolidation: reuse topics, retain evidence, and reject unsafe edits. */
import { describe, expect, it } from "vitest";
import { rm } from "node:fs/promises";
import path from "node:path";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { resolvePlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { accepted, config, draft, expectNeedsReview, job, original, pageId, plan, topicId } from "./knowledge-flow-consolidation-fixtures.js";

/** Preserve the publication assertion while sharing fixture invocation between correction cases. */
async function expectSubmittedSession(runtime: FlowConfig) {
  const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  return result;
}

async function expectFailedDraft(changed: TopicDraft, reason: RegExp, input = job()): Promise<void> {
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed, knowledge_topic_review: accepted() });
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  expect(result).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(reason) });
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

async function expectInvalidQuoteSelector(changed: TopicDraft, message: RegExp): Promise<void> {
  let reviews = 0;
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed,
    knowledge_topic_review: () => { reviews += 1; return accepted(); } });
  const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
  expect(result).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(message) });
  expect(result.contribution).toBeUndefined();
  expect(reviews).toBe(0);
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
    expectNeedsReview(result);
    expect(result.error).toContain("预算单位不明确");
  });

  it("Given a same-name topic in another project, Then cannot route a revision into it", async () => {
    const runtime = config({ knowledge_topic_plan: plan() });
    const result = await consolidateSession(job(), runtime,
      new Map([[pageId, original.replace("projectId: companion", "projectId: other")]]));
    expect(result).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(/project/) });
    expect(result.contribution).toBeUndefined();
  });

  it("Given a cached acceptance missing page review, Then records invalid output instead of retrying the same failure", async () => {
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(),
      knowledge_topic_review: { ...accepted(), checkedPageIds: [] } });
    const first = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(first).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(/omitted/) });
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

  it("Given short approvals and dated source context, When edit and review are prompted, Then both use the shared evidence and publication-time contract", async () => {
    const systems = new Map<string, string>();
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(), knowledge_topic_review: accepted() },
      (system, toolName) => systems.set(toolName, system));
    await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    const edit = systems.get("knowledge_topic_edit") ?? "";
    const review = systems.get("knowledge_topic_review") ?? "";
    for (const clause of ["the user does not need to repeat every parameter", "not independently verified in this batch",
      "pagePublishedAt is the page frontmatter updatedAt value", "Neither a newer observation nor a later page publication alone establishes supersession"]) {
      expect(edit).toContain(clause);
      expect(review).toContain(clause);
    }
    expect(review).toContain("A top-level reject alone never grants source-change permission");
  });

  it("Given a draft deleting historical citations, Then rejects it before publication", async () => {
    const changed = draft(); changed.pages[0].body = "## 当前结论\n预算28个虚构单位。{{claim:0}}";
    await expectFailedDraft(changed, /citation/);
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
    await expectFailedDraft(changed, /review omitted an evidence retirement/);
  });

  it("Given a model invents a GitHub process reference, Then a retirement is held before publishing", async () => {
    const changed = draft(); const invented = "https://github.com/example/wiki/pull/99";
    changed.pages[0].body = `## 当前结论\n预算调整及条件 {{claim:0}}\n\n过程见 ${invented}`;
    changed.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧记录移交外部。", replacement: invented }];
    await expectFailedDraft(changed, /not supported by original evidence/);
  });

  it("Given a hallucinated exact quote, Then the quote-bound schema rejects it before review", async () => {
    const changed = draft(); changed.claims[0].quote = "伪造预算280个虚构单位";
    await expectInvalidQuoteSelector(changed, /quoteId must be equal to one of the allowed values/);
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

  it("Given a session summary alone, Then its quote selector is absent before review", async () => {
    const changed = draft();
    changed.claims[0].supportingQuotes = [{ evidenceId: "summary", quote: "正在讨论样例素材测试预算。" }];
    await expectInvalidQuoteSelector(changed, /supportingQuotes\/0\/quoteId must be equal to one of the allowed values/);
  });

  it("Given a saved specification labeled as a decision, Then it becomes a historical fact that still requires independent review", async () => {
    const input = job(); input.evidence[0].kind = "artifact";
    const changed = draft(); changed.claims[0].kind = "fact"; changed.claims[0].status = "historical";
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: changed,
      knowledge_topic_review: () => { reviews += 1; return accepted(); } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted"); expect(reviews).toBe(1);
    expect(result.contribution?.claims[0]).toMatchObject({ kind: "fact", status: "historical" });
  });

  it("Given a correctable draft error, When corrected using the same original evidence, Then still requires whole-page review", async () => {
    const invalid = draft(); invalid.pages[0].body = "## 当前结论\n{{claim:0}}\n\n此前预算12个虚构单位。^[old.md:1]";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown; evidence?: Array<{ quoteOptions: Array<{ quoteId: string }> }> }) => request.correction
      ? { ...draft(), claims: [{ ...draft().claims[0], quote: undefined, quoteId: request.evidence![0].quoteOptions[0].quoteId }] }
      : invalid,
      knowledge_topic_review: accepted() });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(result.contribution?.evidence.map(item => item.text)).toEqual([job().prompt]);
    expect(result.contribution?.claims[0].evidenceId).toBe("quote-0");
  });

  it("Given an unaccounted old citation, When one correction retires it with a valid claim anchor, Then the page receives full review", async () => {
    const invalid = draft(); invalid.pages[0].body = "## 当前结论\n预算调整 {{claim:0}}";
    const corrected = structuredClone(invalid);
    corrected.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧过程结束", replacement: "{{claim:0}}" }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown }) => request.correction ? corrected : invalid,
      knowledge_topic_review: { ...accepted(), checkedRetiredCitations: ["^[old.md:1]"] } });
    const result = await expectSubmittedSession(runtime);
    expect(result.contribution?.topicRevisions?.[0].citationRetirements?.[0].replacement).toBe("{{claim:0}}");
  });

  it("Given an unaccounted citation, When the correction invents a malformed retirement anchor, Then the correction schema returns a permanent technical failure", async () => {
    const invalid = draft(); invalid.pages[0].body = "## 当前结论\n预算调整 {{claim:0}}";
    const corrected = structuredClone(invalid);
    corrected.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "旧过程结束",
      replacement: "{{claim:0}}、{{claim:2}}" }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown }) => request.correction ? corrected : invalid,
      knowledge_topic_review: accepted() });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result).toMatchObject({ status: "error", retryable: false, error: expect.stringMatching(/correction evidence selection failed|pattern/) });
    expect(result.contribution).toBeUndefined();
  });

  it("Given a correction with a dropped citation, Then it receives the exact unaccounted marker by page", async () => {
    const input = job(); input.evidence[0].kind = "artifact";
    const invalid = draft(); invalid.claims[0].kind = "fact"; invalid.claims[0].status = "historical";
    invalid.pages[0].body = "## 当前结论\n预算28个虚构单位。{{claim:0}}";
    const corrected = structuredClone(invalid);
    corrected.pages[0].body = "## 当前结论\n预算28个虚构单位。{{claim:0}}\n\n{{keep:P1}}";
    let correction: any;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
      if (!request.correction) return invalid;
      correction = request.correction;
      return corrected;
    }, knowledge_topic_review: { ...accepted(), checkedRetiredCitations: [] } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(correction.unaccountedCitations).toEqual([{ pageId, citations: ["^[old.md:1]"] }]);
  });

  it.each(["assistant", "artifact"] as const)("Given a schema-valid %s claim, When the independent review runs, Then its historical authority remains explicit", async (primaryKind) => {
    const input = job(); const primaryText = `${primaryKind} historical evidence`; const supportText = "assistant proposal context";
    input.evidence = [
      { ...input.evidence[0], id: `${primaryKind}-primary`, kind: primaryKind, text: primaryText, sha256: sha256Text(primaryText) },
      { ...input.evidence[0], id: "assistant-support", kind: "assistant", text: supportText, sha256: sha256Text(supportText) },
    ];
    const invalid = draft(); invalid.claims[0] = { ...invalid.claims[0], text: primaryText, evidenceId: `${primaryKind}-primary`, quote: primaryText,
      kind: primaryKind === "assistant" ? "lesson" : "fact", status: "historical", supportingQuotes: [] };
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: invalid,
      knowledge_topic_review: () => { reviews += 1; return accepted(); } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted"); expect(reviews).toBe(1);
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
    expect(result.status).toBe("error");
    expect(result.retryable).toBeUndefined();
    expect(result.error).toMatch(/ENOENT|source/);
  });

  it("Given a new session about the same decision object, Then reuses the project page without prior session associations", async () => {
    const input = { ...job(), id: "another-session", sessionId: "other", sessionContext: undefined };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(), knowledge_topic_review: accepted() });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.contribution?.topicRevisions?.map(page => page.pageId)).toEqual([pageId]);
  });
});
