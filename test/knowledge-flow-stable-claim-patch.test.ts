/** Given/When/Then checks for quoteId-bound stable-claim corrections. */
import { describe, expect, it } from "vitest";
import type { FlowEvidence, FlowResult } from "../extensions/knowledge-flow/types.js";
import { applyCorrectionPatch, correctionPermissionsForReview } from "../extensions/knowledge-flow/claim-patch.js";
import { resolveQuoteBoundDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { QuoteBoundTopicDraft, StableClaimEntry } from "../extensions/knowledge-flow/consolidation-draft.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";
import { sha256Text } from "../src/connectors/hash.js";

const report = "助手报告：样例导入按文件名排序，重复文件会保留最新版本。";
const proposal = "建议先按文件名排序，再检查重复文件。";
const request = "继续";

function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `synthetic:${id}` };
}

function review(decision: "accept" | "reject", count: number, options: Record<string, unknown> = {}) {
  return { ...accepted(), decision, reason: decision === "accept" ? "原文和页面已核验" : "修正主张措辞",
    checkedClaimIndexes: Array.from({ length: count }, (_, index) => index), ...options };
}

function page(body: string, claimIds: string[]) { return { pageId, body, claimIds }; }

/** Invalid corrections must neither retry the cached result nor publish a contribution. */
function expectTechnicalFailure(result: FlowResult): void {
  expect(result).toMatchObject({ status: "error", retryable: false });
  expect(result.contribution).toBeUndefined();
}

function rejectThenAccept(rejectionReason: string, replacementIndexes: number[] = []) {
  let reviews = 0;
  return () => {
    const rejected = reviews++ === 0;
    return review(rejected ? "reject" : "accept", 1, { replaceEvidenceForClaims: rejected ? replacementIndexes : [], claimDecisions: [{ claimIndex: 0,
      decision: rejected ? "reject" : "accept", reason: rejected ? rejectionReason : "通过" }] });
  };
}

describe("stable claim correction patches", () => {
  it("Given repeated selectors for one support quote, When the initial draft resolves, Then evidence is counted once", () => {
    const primary = evidence("primary", "user", "User approves the rule.");
    const supporting = evidence("support", "assistant", "The proposal describes the rule.");
    const catalog = buildCorrectionEvidence([primary, supporting]);
    const primaryQuoteId = catalog.find(item => item.id === primary.id)!.quoteOptions[0].quoteId;
    const supportQuoteId = catalog.find(item => item.id === supporting.id)!.quoteOptions[0].quoteId;
    const quoteDraft: QuoteBoundTopicDraft = { claims: [{ ...draft().claims[0], quoteId: primaryQuoteId,
      supportingQuotes: [{ quoteId: supportQuoteId }, { quoteId: supportQuoteId }, { quoteId: primaryQuoteId }] }],
      pages: draft().pages, summary: "保留批准和提案。" };
    const frozenPage = { pageId, topicId: "topic", title: "样例主题", topic: "样例主题", decisionObject: "样例对象",
      basisHash: "basis", original };

    const resolved = resolveQuoteBoundDraft(quoteDraft, catalog, [frozenPage]);

    expect(resolved.draft.claims[0].supportingQuotes).toEqual([{ evidenceId: supporting.id, quote: supporting.text }]);
    expect(resolved.stableClaims[0].supportingQuoteIds).toEqual([supportQuoteId]);
  });

  it("Given missing or conflicting per-claim decisions, When correction permissions are built, Then unresolved claims stay locked", () => {
    const claims: StableClaimEntry[] = [draft().claims[0], { ...draft().claims[0], text: "第二条规则。" }].map((claim, index) => ({
      claimId: `c${index}`, claim, quoteId: `q-${index}`, supportingQuoteIds: [],
    }));
    expect(correctionPermissionsForReview(claims, {})).toMatchObject({
      lockedClaimIds: ["c0", "c1"], replaceEvidenceForClaimIds: [],
    });
    expect(() => correctionPermissionsForReview(claims, { replaceEvidenceForClaims: [0] })).toThrow(/non-accepted claim/);
    expect(correctionPermissionsForReview(claims, { claimDecisions: [{ claimIndex: 0, decision: "accept" }] }))
      .toMatchObject({ lockedClaimIds: ["c0", "c1"], replaceEvidenceForClaimIds: [] });
    expect(() => correctionPermissionsForReview(claims, { claimDecisions: [
      { claimIndex: 0, decision: "accept" }, { claimIndex: 0, decision: "reject" },
    ], replaceEvidenceForClaims: [0] })).toThrow(/non-accepted claim/);
  });

  it("Given a paraphrase correction and a nearby continue message, When no source change is authorized, Then the assistant report quote stays bound", async () => {
    const input = job();
    const assistant = evidence("report", "assistant", report);
    input.evidence.push(assistant, evidence("continue", "user", request));
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "助手说明重复文件会保留最新版本。", evidenceId: assistant.id,
      quote: assistant.text, kind: "lesson", status: "historical" };
    let reviewCount = 0;
    let correction: any;
    const runtime = config({ knowledge_topic_plan: plan(),
      knowledge_topic_edit: (value: any) => {
        if (!value.correction) return initial;
        correction = value.correction;
        return { claimUpdates: [{ claimId: "c0", changes: [{ field: "text", value: "本批次捕获的助手报告说明重复文件会保留最新版本。" }] }],
          droppedClaimIds: [], pages: [page("## 导入说明\n本批次捕获的助手报告说明重复文件会保留最新版本。{{claim:c0}}\n\n此前内容。^[old.md:1]", ["c0"])],
          summary: "保留来源报告并修正文案。" };
      }, knowledge_topic_review: () => review(++reviewCount === 1 ? "reject" : "accept", 1,
        reviewCount === 1 ? { claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "修正文案" }] }
          : { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "来源和修正文案均有效" }] }) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(correction.previousDraft.claims[0].quoteId).toMatch(/^q-/);
    const narrowedReport = result.contribution?.evidence.find(item => item.locator === assistant.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: narrowedReport?.id, quote: report,
      text: "本批次捕获的助手报告说明重复文件会保留最新版本。" });
    expect(narrowedReport?.locator).toBe("synthetic:report");
  });

  it("Given two claims and a patch that drops c0 while updating c1, When correction resolves, Then c1 keeps its source and maps to numeric claim zero", async () => {
    const input = job();
    const secondEvidence = evidence("artifact-2", "artifact", "样例资料指出重复文件会保留最新版本。\n");
    input.evidence.push(secondEvidence);
    const initial = draft();
    initial.claims.push({ ...initial.claims[0], text: "重复文件会保留最新版本。", evidenceId: secondEvidence.id,
      quote: secondEvidence.text, title: "重复文件规则", slug: "duplicate-file-rule", kind: "fact", status: "historical" });
    initial.pages[0] = { ...initial.pages[0], body: `第一条。{{claim:0}}\n\n第二条。{{claim:1}}\n\n此前内容。^[old.md:1]`, claimIndexes: [0, 1] };
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c1", changes: [{ field: "text", value: "资料说明重复文件会保留最新版本。" }] }], droppedClaimIds: ["c0"],
        pages: [{ ...page("## 文件规则\n资料说明重复文件会保留最新版本。{{claim:c1}}", ["c1"]), citationRetirements: [
          { citation: "^[old.md:1]", reason: "旧样例规则已由当前核验资料替代。", replacement: "{{claim:c1}}" },
        ] }], summary: "只保留仍有依据的文件规则。" }
      : initial, knowledge_topic_review: () => ++reviews === 1
        ? review("reject", 2, { claimDecisions: [0, 1].map(claimIndex => ({ claimIndex, decision: "reject", reason: "需修正" })) })
        : review("accept", 1, { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "保留原来源" }],
          checkedRetiredCitations: ["^[old.md:1]"] }) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims).toHaveLength(1);
    const narrowedSource = result.contribution?.evidence.find(item => item.locator === secondEvidence.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: narrowedSource?.id, quote: narrowedSource?.text,
      text: "资料说明重复文件会保留最新版本。" });
    expect(result.contribution?.topicRevisions?.[0].body).toContain("{{claim:0}}");
    expect(result.contribution?.topicRevisions?.[0].claimIndexes).toEqual([0]);
    expect(result.contribution?.topicRevisions?.[0].citationRetirements?.[0].replacement).toBe("{{claim:0}}");
  });

  it.each([
    { name: "duplicate claim update", patch: { claimUpdates: [{ claimId: "c0", changes: [{ field: "text", value: "a" }] },
      { claimId: "c0", changes: [{ field: "text", value: "b" }] }] } },
    { name: "unknown claim update", patch: { claimUpdates: [{ claimId: "c9", changes: [{ field: "text", value: "a" }] }] } },
    { name: "accepted claim edit", accepted: true, patch: { claimUpdates: [{ claimId: "c0", changes: [{ field: "text", value: "a" }] }] } },
    { name: "accepted claim drop", accepted: true, patch: { claimUpdates: [], droppedClaimIds: ["c0"] } },
  ])("Given a $name, When a correction is resolved, Then the invalid output is a permanent technical failure", async ({ patch, accepted: priorAccepted }) => {
    const initial = draft();
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { ...patch, droppedClaimIds: patch.droppedClaimIds ?? [], pages: [page("## 预算\n预算更新。{{claim:c0}}", ["c0"])], summary: "尝试纠错。" }
      : initial, knowledge_topic_review: () => ++reviews === 1
        ? review("reject", 1, { claimDecisions: [{ claimIndex: 0, decision: priorAccepted ? "accept" : "reject", reason: "发现问题" }] })
        : review("accept", 1, { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "通过" }] }) });

    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expectTechnicalFailure(result);
    expect(result.error).toContain("correction evidence selection failed");
    expect(reviews).toBe(1);
  });

  it("Given an explicit replacement permission, When a user fact moves to lower-authority artifact evidence, Then the exact artifact quote is restored", async () => {
    const input = job();
    const artifact = evidence("artifact-fact", "artifact", "已验证的样例规则。");
    const support = evidence("artifact-support", "artifact", "资料还记录了规则适用的文件范围。");
    input.evidence.push(artifact, support);
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "规则已经核验。", kind: "fact", status: "historical" };
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [
        { field: "quoteId", value: quoteId(value, artifact.id) }, { field: "text", value: "资料记录了已验证的样例规则。" },
        { field: "supportingQuoteIds", value: [quoteId(value, support.id), quoteId(value, support.id)] },
      ] }],
        droppedClaimIds: [], pages: [page("## 规则\n资料记录了已验证的样例规则。{{claim:c0}}\n\n此前内容。^[old.md:1]", ["c0"])], summary: "按来源修正规则。" }
      : initial, knowledge_topic_review: () => ++reviews === 1
        ? review("reject", 1, { replaceEvidenceForClaims: [0], claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "原主引文不是来源记录" }] })
        : review("accept", 1, { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "来源支持" }] }) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    const narrowedSource = result.contribution?.evidence.find(item => item.locator === artifact.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: narrowedSource?.id, quote: artifact.text });
    const narrowedSupport = result.contribution?.evidence.find(item => item.locator === support.locator);
    expect(result.contribution?.claims[0].supportingQuotes).toEqual([{ evidenceId: narrowedSupport?.id, quote: support.text }]);
  });

  it("Given a brief approval and its assistant proposal, When wording alone is corrected, Then both exact references stay together", async () => {
    const input = job();
    const approval = evidence("approval", "user", "同意这个方案。");
    const assistant = evidence("proposal", "assistant", proposal);
    input.evidence = [approval, assistant];
    input.prompt = approval.text;
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: proposal, evidenceId: approval.id, quote: approval.text,
      supportingQuotes: [{ evidenceId: assistant.id, quote: assistant.text }] };
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "text", value: "用户同意了助手提出的文件检查方案。" }] }], droppedClaimIds: [],
        pages: [page("## 文件检查\n用户同意了助手提出的文件检查方案。{{claim:c0}}\n\n此前内容。^[old.md:1]", ["c0"])], summary: "保留批准及原提案。" }
      : initial, knowledge_topic_review: () => ++reviews === 1
        ? review("reject", 1, { claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "补充说明批准对象" }] })
        : review("accept", 1, { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "批准和提案共同支持" }] }) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    const narrowedApproval = result.contribution?.evidence.find(item => item.locator === approval.locator);
    const narrowedProposal = result.contribution?.evidence.find(item => item.locator === assistant.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: narrowedApproval?.id, quote: approval.text,
      supportingQuotes: [{ evidenceId: narrowedProposal?.id, quote: assistant.text }] });
  });

  it("Given no source-change permission, When a patch drops assistant support, Then the correction is a permanent technical failure", async () => {
    const input = job();
    const assistant = evidence("proposal-to-drop", "assistant", proposal);
    input.evidence.push(assistant);
    const initial = draft();
    initial.claims[0].supportingQuotes = [{ evidenceId: assistant.id, quote: assistant.text }];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "supportingQuoteIds", value: [] }] }], droppedClaimIds: [],
        pages: [page("## 文件检查\n保留用户批准的方案。{{claim:c0}}\n\n此前内容。^[old.md:1]", ["c0"])], summary: "尝试修改支持证据。" }
      : initial, knowledge_topic_review: rejectThenAccept("修正文案") });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expectTechnicalFailure(result);
  });

  it("Given replacement permission for an assistant lesson, When the patch selects user evidence, Then authority promotion is rejected", async () => {
    const input = job();
    const assistant = evidence("assistant-lesson", "assistant", report);
    input.evidence.push(assistant);
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "助手报告说明重复文件规则。", evidenceId: assistant.id,
      quote: assistant.text, kind: "lesson", status: "historical" };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, "user-2") }] }], droppedClaimIds: [],
        pages: [page("## 预算\n助手报告说明重复文件规则。{{claim:c0}}\n\n此前内容。^[old.md:1]", ["c0"])], summary: "切换引文。" }
      : initial, knowledge_topic_review: rejectThenAccept("需要修正主引文", [0]) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expectTechnicalFailure(result);
    expect(result.error).toContain("must be equal to one of the allowed values");
  });

  it("Given source-change permission, When runtime receives a user quote for an assistant claim, Then it rejects authority promotion", () => {
    const assistant = evidence("report", "assistant", report);
    const user = evidence("approval", "user", "用户批准执行。");
    const catalog = buildCorrectionEvidence([assistant, user]);
    const entry: StableClaimEntry = { claimId: "c0", quoteId: catalog[0].quoteOptions[0].quoteId, supportingQuoteIds: [],
      claim: { ...draft().claims[0], evidenceId: assistant.id, quote: report, kind: "lesson", status: "historical" } };
    const patch = { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId" as const, value: catalog[1].quoteOptions[0].quoteId }] }],
      droppedClaimIds: [], pages: [page("报告内容。{{claim:c0}}", ["c0"])], summary: "更换来源。" };
    const permissions = { lockedClaimIds: [], replaceEvidenceForClaimIds: ["c0"] };

    expect(() => applyCorrectionPatch(patch, [entry], permissions, catalog, [])).toThrow(/cannot promote source authority/);
  });
});

function quoteId(request: Record<string, any>, evidenceId: string): string {
  return request.evidence.find((item: any) => item.id === evidenceId).quoteOptions[0].quoteId;
}
