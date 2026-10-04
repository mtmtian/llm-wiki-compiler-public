/**
 * A correction must not move a claim to unrelated evidence. Corrections pick evidence from frozen quote
 * options, which can accidentally pin claims to an unrelated user message even when the first draft
 * cited the right evidence. When a corrected claim is clearly the same
 * claim as before, its original evidence is restored: the original exact quote, or the original evidence's
 * option that overlaps the previous quote most. Rewritten or ambiguous claims, and claims the previous
 * review did not accept and did not explicitly retain, keep the correction's choice.
 */
import { describe, expect, it } from "vitest";
import { preserveEvidence } from "../extensions/knowledge-flow/quote-repair.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const evidence = (id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence =>
  ({ id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `codex://s/${id}` });
const report = "## 发布结果\n样例服务 v2 已合并并上线，部署后回读样例数据正常，状态为 ready。\n120 个样例请求均未因缺少虚构计数而被拒绝。\n";
const items = [evidence("u0", "user", "<task-notification>后台任务完成</task-notification>"),
  evidence("a1", "assistant", report), evidence("u2", "user", "继续")];
const context = { evidence: items, catalog: buildCorrectionEvidence(items), disputed: new Set<number>() };
const lesson = (text: string, evidenceId: string, quote: string) =>
  ({ ...draft().claims[0], text, evidenceId, quote, kind: "lesson" as const, status: "historical" as const });
const withClaims = (claims: TopicDraft["claims"]): TopicDraft => ({ ...draft(), claims });
const first = "样例服务 v2 上线后样例数据正常，状态为 ready。";
const second = "120 个样例请求均未因缺少虚构计数而被拒绝。";
const exactFirst = "样例服务 v2 已合并并上线，部署后回读样例数据正常，状态为 ready。";

/** A two-claim draft whose correction pins every claim to the first quote option; returns claim evidence kinds. */
async function pinnedCorrection(secondQuote: string, review: () => unknown): Promise<unknown[]> {
  const input = job();
  input.sessionContext!.evidence = [items[0]];
  input.evidence.push(items[1]);
  const initial = draft();
  initial.claims.push(lesson(second, "a1", secondQuote));
  initial.pages[0] = { ...initial.pages[0], body: `${initial.pages[0].body}\n\n120 个样例请求未被拒绝。{{claim:1}}`, claimIndexes: [0, 1] };
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: review,
    knowledge_topic_edit: (request: any) => request.correction
      ? { ...initial, claims: initial.claims.map(claim => ({ ...claim, quoteId: request.evidence[0].quoteOptions[0].quoteId })) }
      : initial });
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  return result.contribution!.claims.map(claim => result.contribution!.evidence.find(item => item.id === claim.evidenceId)?.kind);
}

describe("evidence preservation across a correction", () => {
  it("Given a correction that moved unchanged claims to the first option, Then their original exact quotes return", () => {
    const previous = withClaims([lesson(first, "a1", exactFirst)]);
    const corrected = withClaims([lesson(first, "u0", items[0].text)]);
    expect(preserveEvidence(corrected, previous, context).claims[0]).toMatchObject({ evidenceId: "a1", quote: exactFirst });
  });

  it("Given an inexact previous quote, Then the best-overlapping option of the original evidence is used", () => {
    const previous = withClaims([lesson(second, "a1", "共 120 个样例请求均未因缺少虚构计数而被拒绝")]);
    const corrected = withClaims([lesson(`${second} `, "u2", "继续")]);
    const [claim] = preserveEvidence(corrected, previous, context).claims;
    expect(claim.evidenceId).toBe("a1");
    expect(report.includes(claim.quote) && claim.quote.includes("120 个样例请求均未因缺少虚构计数而被拒绝")).toBe(true);
  });

  it("Given a rewritten claim or an ambiguous match, Then the correction's own choice stays", () => {
    const previous = withClaims([lesson(first, "a1", "状态为 ready。"), lesson(`${first}！`, "a1", "状态为 ready。")]);
    const rewritten = withClaims([lesson("历史样例回填已完成，数据可用于虚构报表。", "u2", "继续")]);
    expect(preserveEvidence(rewritten, previous, context)).toEqual(rewritten);
    const ambiguous = withClaims([lesson(first, "u2", "继续")]);
    expect(preserveEvidence(ambiguous, previous, context)).toEqual(ambiguous);
  });

  it("Given a claim the previous review did not accept, Then the correction may move it to other evidence", () => {
    const previous = withClaims([lesson(first, "a1", exactFirst)]);
    const corrected = withClaims([lesson(first, "u2", "继续")]);
    expect(preserveEvidence(corrected, previous, { ...context, disputed: new Set([0]) })).toEqual(corrected);
  });
});

describe("reviewed evidence retention", () => {
  it("Given review requests wording changes while retaining evidence, Then rejected claims keep their original references", () => {
    const previous = withClaims([lesson(`助手在采集日期报告：${first}`, "a1", exactFirst)]);
    const corrected = withClaims([lesson(`本批次捕获的助手报告：${first}`, "u2", "继续")]);
    const retained = { ...context, disputed: new Set([0]), retained: new Set([0]) };
    expect(preserveEvidence(corrected, previous, retained).claims[0]).toMatchObject({ evidenceId: "a1", quote: exactFirst });
  });

  it("Given retained evidence and a different passage in the same message, Then the exact original quote survives", () => {
    const previous = withClaims([lesson(first, "a1", exactFirst)]);
    const corrected = withClaims([lesson(first, "a1", second)]);
    const retained = { ...context, disputed: new Set([0]), retained: new Set([0]) };
    expect(preserveEvidence(corrected, previous, retained).claims[0].quote).toBe(exactFirst);
  });

  it("Given similar words on a different destination, Then retention cannot transfer evidence across pages", () => {
    const previous = withClaims([lesson(first, "a1", exactFirst)]);
    const corrected = withClaims([{ ...lesson(first, "u2", "继续"), targetPageId: "concepts/another-topic" }]);
    expect(preserveEvidence(corrected, previous, context)).toEqual(corrected);
  });

  it("Given retained approval with supporting terms, Then correction cannot drop the approved proposal", () => {
    const previous = withClaims([{ ...lesson(first, "u2", "继续"), supportingQuotes: [{ evidenceId: "a1", quote: exactFirst }] }]);
    const corrected = withClaims([lesson(first, "u2", "继续")]);
    const retained = { ...context, disputed: new Set([0]), retained: new Set([0]) };
    expect(preserveEvidence(corrected, previous, retained).claims[0].supportingQuotes).toEqual(previous.claims[0].supportingQuotes);
  });
});

describe("consolidation evidence preservation", () => {
  it("Given a correction that pins every claim to the first option, When consolidated, Then claims keep their own evidence", async () => {
    const kinds = await pinnedCorrection("共 120 个样例请求均未因缺少虚构计数而被拒绝", () => ({ ...accepted(), checkedClaimIndexes: [0, 1] }));
    expect(kinds).toEqual(["user", "assistant"]);
  });

  it("Given a review that rejected one claim, When corrected, Then only the accepted claim keeps its evidence", async () => {
    let reviews = 0;
    const kinds = await pinnedCorrection(second, () => reviews++ === 0
      ? { ...accepted(), decision: "reject", reason: "第 2 条的证据不支持结论", checkedClaimIndexes: [0, 1],
        claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "原文支持" }, { claimIndex: 1, decision: "reject", reason: "证据不支持" }] }
      : { ...accepted(), checkedClaimIndexes: [0, 1] });
    expect(reviews).toBe(2);
    expect(kinds).toEqual(["user", "user"]);
  });
});
