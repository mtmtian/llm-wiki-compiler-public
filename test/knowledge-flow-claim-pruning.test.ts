/**
 * Partial page publication. A final review that rejects some claims used to hold the whole batch even
 * when it accepted the rest. The lines citing rejected claims are now removed, the remaining claims are
 * renumbered, and the reduced draft is validated and reviewed again before it can publish. Anything that
 * cannot be removed cleanly (a rejected claim sharing a line with an accepted claim or an existing
 * citation, an incomplete review) keeps the batch held as before.
 */
import { describe, expect, it } from "vitest";
import { withoutRejectedClaims } from "../extensions/knowledge-flow/claim-pruning.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { ClaimReview, FlowResult } from "../extensions/knowledge-flow/types.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const scope = "样例素材只在虚构渠道 A 测试。";
const body = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 投放范围\n只在虚构渠道 A 测试。{{claim:1}}\n\n## 决策历史\n此前预算 12 个虚构单位。^[old.md:1]";
const verdicts = (...decisions: string[]) => decisions.map((decision, claimIndex) => ({ claimIndex, decision, reason: decision === "accept" ? "原文支持" : "证据不支持" }));
const review = (...decisions: string[]): ClaimReview =>
  ({ stage: "correction", decision: "reject", complete: true, claims: verdicts(...decisions) as ClaimReview["claims"] });

/** The fixture draft plus a second claim on its own line under its own heading. */
function twoClaims(): TopicDraft {
  const value = draft();
  value.claims.push({ ...value.claims[0], text: scope, quote: scope, evidenceId: "user-3", title: "投放范围" });
  value.pages[0] = { ...value.pages[0], body, claimIndexes: [0, 1] };
  return value;
}

describe("partial page publication: removing rejected claims", () => {
  it("Given one rejected claim, Then its lines and emptied heading go and the rest is renumbered", () => {
    const value = twoClaims();
    value.claims.unshift({ ...value.claims[1], text: "被拒的说法" });
    value.pages[0] = { ...value.pages[0], body: `被拒的说法。{{claim:0}}\n${body.replace("{{claim:1}}", "{{claim:2}}").replace("{{claim:0}}", "{{claim:1}}")}`, claimIndexes: [0, 1, 2] };
    const pruned = withoutRejectedClaims(value, review("reject", "accept", "accept"))!;
    expect(pruned.claims.map(claim => claim.text)).toEqual([value.claims[1].text, scope]);
    expect(pruned.pages[0]).toMatchObject({ body, claimIndexes: [0, 1] });
  });

  it("Given a rejected claim alone under a heading, Then the heading goes but an already empty section stays", () => {
    const value = twoClaims();
    value.pages[0].body = `## 待定\n\n${body}`;
    const pruned = withoutRejectedClaims(value, review("accept", "reject"))!;
    expect(pruned.pages[0].body).toBe("## 待定\n\n## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n此前预算 12 个虚构单位。^[old.md:1]");
    expect(pruned.claims).toHaveLength(1);
  });

  it("Given an incomplete review or nothing (or everything) rejected, Then nothing is pruned", () => {
    expect(withoutRejectedClaims(twoClaims(), { ...review("accept", "reject"), complete: false })).toBeNull();
    expect(withoutRejectedClaims(twoClaims(), review("accept", "accept"))).toBeNull();
    expect(withoutRejectedClaims(twoClaims(), review("reject", "needs_review"))).toBeNull();
    expect(withoutRejectedClaims(twoClaims(), undefined)).toBeNull();
  });

  it("Given a rejected claim sharing a line with an accepted claim or an existing citation, Then nothing is pruned", () => {
    const shared = twoClaims();
    shared.pages[0].body = "预算 28 个虚构单位。{{claim:0}} 只在虚构渠道 A 测试。{{claim:1}}";
    expect(withoutRejectedClaims(shared, review("accept", "reject"))).toBeNull();
    const cited = twoClaims();
    cited.pages[0].body = body.replace("测试。{{claim:1}}", "测试。{{claim:1}} ^[old.md:1]");
    expect(withoutRejectedClaims(cited, review("accept", "reject"))).toBeNull();
  });

  it("Given a page whose claims were all rejected, Then that page keeps its current text", () => {
    const value = twoClaims();
    value.pages[0] = { ...value.pages[0], body: "预算 28 个虚构单位。{{claim:0}}", claimIndexes: [0] };
    value.pages.push({ pageId: "concepts/other", body: "只在虚构渠道 A 测试。{{claim:1}}", claimIndexes: [1] });
    expect(withoutRejectedClaims(value, review("accept", "reject"))!.pages.map(page => page.pageId)).toEqual([pageId]);
  });
});

/** Consolidate the two-claim draft; every review rejects claim 1 until `pruned` answers the third review. */
async function consolidateWithRejectedClaim(pruned: unknown): Promise<{ result: FlowResult; requests: any[] }> {
  const input = job();
  input.evidence.push({ id: "user-3", kind: "user", text: scope, sha256: sha256Text(scope),
    observedAt: "2025-01-15T00:00:00Z", locator: "codex://s/t3" });
  const requests: any[] = [];
  const rejectOne = { decision: "reject", reason: "第 2 条证据不支持", checkedClaimIndexes: [0, 1], checkedPageIds: [pageId],
    claimDecisions: verdicts("accept", "reject") };
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: twoClaims(),
    knowledge_topic_review: (request: any) => { requests.push(request); return requests.length < 3 ? rejectOne : pruned; } });
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  return { result, requests };
}

const scopePage = { action: "create", targetPageId: null, topic: "样例素材推广", decisionObject: "样例素材投放范围",
  title: "样例素材投放范围", reason: "投放范围是独立的决策对象" };

/** Plan an update of the budget page plus a new scope page that only rejected claim 1 revises. */
async function consolidateTwoPages(pruned: unknown): Promise<{ result: FlowResult; requests: any[] }> {
  const input = job();
  input.evidence.push({ id: "user-3", kind: "user", text: scope, sha256: sha256Text(scope),
    observedAt: "2025-01-15T00:00:00Z", locator: "codex://s/t3" });
  const edit = (request: any) => {
    const value = draft();
    const created = request.pages[1].pageId;
    value.claims.push({ ...value.claims[0], text: scope, quote: scope, evidenceId: "user-3", title: "投放范围",
      targetPageId: created, decisionObject: scopePage.decisionObject });
    value.pages.push({ pageId: created, body: "## 投放范围\n只在虚构渠道 A 测试。{{claim:1}}", claimIndexes: [1] });
    return value;
  };
  const requests: any[] = [];
  const rejectOne = (request: any) => ({ decision: "reject", reason: "第 2 条证据不支持", checkedClaimIndexes: [0, 1],
    checkedPageIds: request.pages.map((page: any) => page.pageId), claimDecisions: verdicts("accept", "reject") });
  const runtime = config({ knowledge_topic_plan: { ...plan(), pages: [...plan().pages, scopePage] }, knowledge_topic_edit: edit,
    knowledge_topic_review: (request: any) => { requests.push(request); return requests.length < 3 ? rejectOne(request) : pruned; } });
  return { result: await consolidateSession(input, runtime, new Map([[pageId, original]])), requests };
}

describe("partial page publication: end to end", () => {
  it("Given the only claim of a planned new page is rejected, Then that page is not created and the rest publishes", async () => {
    const { result, requests } = await consolidateTwoPages(accepted());
    expect(result.status).toBe("submitted");
    expect(result.contribution!.topicRevisions!.map(page => page.pageId)).toEqual([pageId]);
    expect(result.contribution!.claims.map(claim => claim.text)).toEqual([job().prompt]);
    expect(requests[2].pages.map((page: any) => page.pageId)).toEqual([pageId]);
    expect(requests[2].plan.pages.map((page: any) => page.title)).toEqual([plan().pages[0].title]);
  });

  it("Given a final review that rejects one claim, When the reduced draft passes a fresh review, Then it publishes without that claim", async () => {
    const { result, requests } = await consolidateWithRejectedClaim(accepted());
    expect(result.status).toBe("submitted");
    expect(requests.map(request => request.claims.length)).toEqual([2, 2, 1]);
    expect(result.contribution!.claims.map(claim => claim.text)).toEqual([job().prompt]);
    expect(result.contribution!.topicRevisions![0].body).not.toContain("投放范围");
    expect(result.claimReviews!.map(item => item.stage)).toEqual(["initial", "correction", "pruned"]);
  });

  it("Given the reduced draft is rejected too, Then the batch stays held with both reasons", async () => {
    const { result } = await consolidateWithRejectedClaim({ ...accepted(), decision: "reject", reason: "页面叙述仍不完整" });
    expect(result.status).toBe("needs_review");
    expect(result.error).toContain("第 2 条证据不支持");
    expect(result.error).toContain("只保留已接受的 claim 后仍未通过审核：页面叙述仍不完整");
  });

  it("Given the fresh review fails, Then the batch is held as before instead of erroring", async () => {
    const { result } = await consolidateWithRejectedClaim({ ...accepted(), checkedClaimIndexes: [0, 1] });
    expect(result.status).toBe("needs_review");
    expect(result.error).toMatch(/^第 2 条证据不支持；只保留已接受的 claim 后审核失败：knowledge_topic_review: /);
    expect(result.claimReviews!.map(item => item.stage)).toEqual(["initial", "correction"]);
  });
});
