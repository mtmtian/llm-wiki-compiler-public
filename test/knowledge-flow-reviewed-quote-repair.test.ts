/** Given/When/Then checks for bounded reviewer-suggested primary quote repair. */
import { describe, expect, it } from "vitest";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const secondText = "沿用原有样例范围。";
const secondEvidence = evidence("user-scope", "user", secondText, "2025-01-15T00:00:00Z");

function evidence(id: string, kind: FlowEvidence["kind"], text: string, observedAt: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt, locator: `turn:${id}` };
}

function twoClaimFixture() {
  const input = job();
  const firstText = input.evidence[0].text;
  input.evidence.push(secondEvidence);
  input.sessionContext!.evidence = [evidence("older-user", "user", firstText, "2024-01-02T00:00:00Z")];
  const changed = draft();
  changed.claims[0].evidenceId = "older-user";
  changed.claims.push({ ...changed.claims[0], text: secondText, evidenceId: secondEvidence.id, quote: secondText,
    title: "适用范围", slug: "sample-scope", kind: "constraint", status: "historical", rationale: "保留原范围" });
  changed.pages[0].body = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n沿用原有样例范围。{{claim:1}}\n\n## 决策历史\n此前预算 12 个虚构单位。^[old.md:1]";
  changed.pages[0].claimIndexes = [0, 1];
  return { input, changed };
}

function twoClaimAccept() {
  return { ...accepted(), checkedClaimIndexes: [0, 1], claimDecisions: [
    { claimIndex: 0, decision: "accept", reason: "原文直接支持" },
    { claimIndex: 1, decision: "accept", reason: "原文直接支持" },
  ] };
}

function reviewForTwo(decisions: Array<{ claimIndex: number; decision: "accept" | "reject" | "needs_review" }>,
  quoteRepairs?: Array<{ claimIndex: number; quoteId: string }>) {
  return { ...twoClaimAccept(), decision: "reject", reason: "一条主引文未对应本条主张",
    claimDecisions: decisions.map(item => ({ ...item, reason: "主引文对应错误" })),
    ...(quoteRepairs ? { quoteRepairs } : {}) };
}

function currentQuoteId(input: ReturnType<typeof twoClaimFixture>["input"], request: Record<string, any>): string {
  const text = input.evidence[0].text;
  return request.evidence.find((item: any) => item.id === input.evidence[0].id).quoteOptions
    .find((option: any) => option.quote === text).quoteId;
}

function expectedClaim(claim: ReturnType<typeof draft>["claims"][number], evidenceId: string) {
  const copy = structuredClone(claim);
  copy.evidenceId = evidenceId;
  if (!copy.supportingQuotes?.length) delete copy.supportingQuotes;
  return copy;
}

async function editorCorrection(input: ReturnType<typeof job>, changed: ReturnType<typeof draft>,
  firstReview: (request: Record<string, any>) => unknown, finalReview: () => unknown = twoClaimAccept) {
  let edits = 0;
  let reviews = 0;
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: () => { edits += 1; return changed; },
    knowledge_topic_review: (request: Record<string, any>) => ++reviews === 1 ? firstReview(request) : finalReview() });
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  expect([edits, reviews]).toEqual([2, 2]);
  return result;
}

describe("reviewed quote repair", () => {
  it("Given one rejected and one accepted claim, When review proposes an exact quote, Then only that binding changes and the full page is reviewed once more", async () => {
    const { input, changed } = twoClaimFixture();
    let edits = 0;
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: () => { edits += 1; return changed; },
      knowledge_topic_review: (request: Record<string, any>) => {
        reviews += 1;
        if (reviews === 1) {
          expect(request.evidence[0]).not.toHaveProperty("text");
          expect(request.evidence[0].quoteOptions[0].quote).toBe(input.evidence[0].text);
          expect(request.evidence.find((item: any) => item.id === "older-user").observedAt).toBe("2024-01-02T00:00:00Z");
          return reviewForTwo([{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "accept" }],
            [{ claimIndex: 0, quoteId: currentQuoteId(input, request) }]);
        }
        expect(request.claims[0].evidenceId).toBe(input.evidence[0].id);
        return twoClaimAccept();
      } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(edits).toBe(1);
    expect(reviews).toBe(2);
    expect(result.claimReviews?.map(item => item.stage)).toEqual(["initial", "correction"]);
    expect(result.contribution?.topicRevisions?.[0].body).toBe(changed.pages[0].body);
    const currentPublishedId = result.contribution?.evidence.find(item => item.locator === input.evidence[0].locator)?.id;
    const secondPublishedId = result.contribution?.evidence.find(item => item.locator === secondEvidence.locator)?.id;
    expect(result.contribution?.claims[0]).toEqual(expectedClaim({ ...changed.claims[0],
      evidenceId: input.evidence[0].id, quote: input.evidence[0].text }, currentPublishedId!));
    expect(result.contribution?.claims[1]).toEqual(expectedClaim(changed.claims[1], secondPublishedId!));
  });

  it("Given a decision claim, When the reviewer suggests an assistant quote, Then the suggestion cannot bypass the editor correction", async () => {
    const input = job();
    const assistant = evidence("assistant-proposal", "assistant", "建议预算为28个虚构单位。", "2024-04-03T00:00:00Z");
    input.sessionContext!.evidence = [assistant];
    const starting = draft();
    const result = await editorCorrection(input, starting, request => ({ ...accepted(), decision: "reject", reason: "主引文错误",
        claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "需用户主引文" }],
        quoteRepairs: [{ claimIndex: 0, quoteId: request.evidence.find((item: any) => item.id === assistant.id).quoteOptions[0].quoteId }] }), accepted);
    const currentId = result.contribution?.evidence.find(item => item.locator === input.evidence[0].locator)?.id;
    expect(result.contribution?.claims[0].evidenceId).toBe(currentId);
  });

  it.each([
    { name: "out-of-range", decisions: [{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "needs_review" }], indexes: [0, 2] },
    { name: "duplicate", decisions: [{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "needs_review" }], indexes: [0, 0] },
    { name: "incomplete", decisions: [{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "needs_review" }], indexes: [0] },
    { name: "missing claim verdict", decisions: [{ claimIndex: 0, decision: "reject" }], indexes: [0] },
    { name: "accepted claim targeted", decisions: [{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "accept" }], indexes: [0, 1] },
  ] as const)("Given a $name suggestion, When the editor can still correct it, Then the suggestion does not auto-apply", async ({ decisions, indexes }) => {
    const { input, changed } = twoClaimFixture();
    const current = buildCorrectionEvidence([...input.sessionContext!.evidence, ...input.evidence]);
    const currentQuote = current.find(item => item.id === input.evidence[0].id)!.quoteOptions[0].quoteId;
    await editorCorrection(input, changed, () => reviewForTwo([...decisions],
      indexes.map(claimIndex => ({ claimIndex, quoteId: currentQuote }))));
  });

  it("Given an unknown quoteId, When review output is schema-checked against the frozen catalog, Then the batch cannot publish", async () => {
    const { input, changed } = twoClaimFixture();
    let edits = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: () => { edits += 1; return changed; },
      knowledge_topic_review: () => reviewForTwo([{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "accept" }],
        [{ claimIndex: 0, quoteId: "q-not-in-the-frozen-catalog" }]) });
    await expect(consolidateSession(input, runtime, new Map([[pageId, original]]))).resolves.toMatchObject({
      status: "error", retryable: false, error: expect.stringMatching(/quoteId.*allowed value/),
    });
    expect(edits).toBe(1);
  });

  it("Given a valid quote repair whose final review still rejects, When the bounded run ends, Then nothing is published", async () => {
    const { input, changed } = twoClaimFixture();
    const currentCatalog = buildCorrectionEvidence([...input.sessionContext!.evidence, ...input.evidence]);
    const quoteId = currentCatalog.find(item => item.id === input.evidence[0].id)!.quoteOptions[0].quoteId;
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: () => changed,
      knowledge_topic_review: () => {
        reviews += 1;
        return reviews === 1
          ? reviewForTwo([{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "reject" }],
            [{ claimIndex: 0, quoteId }, { claimIndex: 1, quoteId }])
          : reviewForTwo([{ claimIndex: 0, decision: "reject" }, { claimIndex: 1, decision: "reject" }]);
      } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result).toMatchObject({ status: "error", retryable: false });
    expect(result.contribution).toBeUndefined();
    expect(reviews).toBe(2);
  });

  it("Given a published page with an older source, When edit and review run, Then page publication time and evidence capture time remain distinct", async () => {
    const input = job();
    const assistant = evidence("assistant-analysis", "assistant", "建议预算为28个虚构单位。", "2022-03-04T00:00:00Z");
    input.sessionContext!.evidence = [assistant];
    const changed = draft();
    changed.claims[0].supportingQuotes = [{ evidenceId: assistant.id, quote: assistant.text }];
    const datedPage = original.replace("knowledgeTopicId:", "updatedAt: 2024-06-07T00:00:00Z\nknowledgeTopicId:");
    let editDate: unknown;
    let reviewDate: unknown;
    let reviewEvidence: any[] = [];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: Record<string, any>) => {
      editDate = request.pages[0].pagePublishedAt;
      return changed;
    }, knowledge_topic_review: (request: Record<string, any>) => {
      reviewDate = request.pages[0].pagePublishedAt;
      reviewEvidence = request.evidence;
      return accepted();
    } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, datedPage]]));
    expect(result.status).toBe("submitted");
    expect(editDate).toBe("2024-06-07T00:00:00.000Z");
    expect(reviewDate).toBe(editDate);
    expect(reviewEvidence.find(item => item.id === assistant.id).observedAt).toBe("2022-03-04T00:00:00Z");
    expect(reviewEvidence.find(item => item.id === assistant.id)).not.toHaveProperty("text");
    expect(result.contribution?.evidence.find(item => item.locator === assistant.locator)?.observedAt).toBe("2022-03-04T00:00:00Z");
  });
});
