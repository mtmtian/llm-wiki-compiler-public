/**
 * Per-claim review conclusions are recorded for observation while the ledger gate is closed
 * (deployment/KNOWLEDGE-LEDGER.md §7.2, step B2): the page-level review still decides every
 * outcome, and each review attempt's per-claim conclusions travel on the result.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { createTopicReviewTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import type { FlowResult } from "../extensions/knowledge-flow/types.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const keep = { claimIndex: 0, decision: "accept", reason: "用户原话直接支持新预算" };
const drop = { claimIndex: 0, decision: "reject", reason: "原文只是询问，不是决定" };

async function consolidate(...reviews: unknown[]): Promise<FlowResult> {
  let call = 0;
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: draft(),
    knowledge_topic_review: () => reviews[Math.min(call++, reviews.length - 1)] });
  return consolidateSession(job(), runtime, new Map([[pageId, original]]));
}

describe("per-claim review conclusions with the ledger gate closed", () => {
  it("Given an accepting review, Then the batch is submitted and its per-claim conclusions are recorded", async () => {
    const result = await consolidate({ ...accepted(), claimDecisions: [keep] });
    expect(result.status).toBe("submitted");
    expect(result.claimReviews).toEqual([{ stage: "initial", decision: "accept", complete: true, claims: [keep] }]);
  });

  it("Given a held page with an acceptable claim, Then the batch stays held and records the partial acceptance", async () => {
    const result = await consolidate({ ...accepted(), decision: "needs_review", reason: "页面合并需人工确认", claimDecisions: [keep] });
    expect(result.status).toBe("needs_review");
    expect(result.contribution).toBeUndefined();
    expect(result.claimReviews?.[0]).toMatchObject({ decision: "needs_review", complete: true, claims: [keep] });
  });

  it("Given an accepted page with a rejected claim, Then the page decision still decides the outcome", async () => {
    const result = await consolidate({ ...accepted(), claimDecisions: [drop] });
    expect(result.status).toBe("submitted");
    expect(result.claimReviews?.[0].claims).toEqual([drop]);
  });

  it("Given missing or incomplete per-claim conclusions, Then the outcome is unchanged and the record is marked incomplete", async () => {
    for (const claimDecisions of [undefined, [], [keep, keep]]) {
      const result = await consolidate({ ...accepted(), ...(claimDecisions ? { claimDecisions } : {}) });
      expect(result.status).toBe("submitted");
      expect(result.claimReviews?.[0].complete).toBe(false);
    }
  });

  it("Given a rejected first review and an accepted correction, Then both review attempts are recorded in order", async () => {
    const result = await consolidate({ ...accepted(), decision: "reject", reason: "请改写措辞", claimDecisions: [drop] },
      { ...accepted(), claimDecisions: [keep] });
    expect(result.status).toBe("submitted");
    expect(result.claimReviews?.map(review => [review.stage, review.decision])).toEqual([["initial", "reject"], ["correction", "accept"]]);
  });

  it("Given a review tool for this run, Then it asks for conclusions on this run's claims without making the review depend on them", () => {
    const schema = createTopicReviewTool(2, [pageId], []).input_schema as Record<string, any>;
    expect(schema.required).not.toContain("claimDecisions");
    const conclusion = schema.properties.claimDecisions.items;
    expect(conclusion.required).toEqual(["claimIndex", "decision", "reason"]);
    expect(conclusion.properties.claimIndex.enum).toEqual([0, 1]);
    expect(conclusion.properties.decision.enum).toEqual(["accept", "reject", "needs_review"]);
  });
});
