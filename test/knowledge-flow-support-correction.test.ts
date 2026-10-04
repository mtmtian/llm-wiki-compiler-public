/**
 * Invalid supporting references must remain repairable before the first independent review.
 * Once a review accepts the evidence, a prose correction must retain those supporting terms.
 * Scripted providers exercise validation, correction and the final independent review together.
 */
import { expect, it } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowClaim } from "../extensions/knowledge-flow/types.js";
import { accepted, draft, job, original, pageId, plan, runConsolidation } from "./knowledge-flow-consolidation-fixtures.js";

const proposal = "建议将样例预算调整为28个虚构单位，并保留复核日志。";
const support = { evidenceId: "proposal", quote: proposal };

/** Run a supporting-reference correction, recording exactly which drafts reach review. */
async function correctSupportingReferences(initialSupport: FlowClaim["supportingQuotes"], reviewedFirst = false) {
  const input = job();
  input.evidence.push({ id: support.evidenceId, kind: "assistant", text: proposal, sha256: sha256Text(proposal),
    observedAt: input.createdAt, locator: "synthetic:proposal" });
  const initial = draft();
  initial.claims[0].supportingQuotes = initialSupport;
  const corrected = draft();
  corrected.claims[0].supportingQuotes = reviewedFirst ? [] : [support];
  const reviewed: FlowClaim[][] = [];
  let correction: Record<string, unknown> | undefined;
  const result = await runConsolidation(input, { knowledge_topic_plan: plan(),
    knowledge_topic_edit: (request: { correction?: Record<string, unknown> }) => {
      correction = request.correction;
      return request.correction ? corrected : initial;
    },
    knowledge_topic_review: (request: { claims: FlowClaim[] }) => {
      reviewed.push(request.claims);
      return { ...accepted(), decision: reviewedFirst && reviewed.length === 1 ? "reject" : "accept",
        claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "主张与支持条款均有来源" }] };
    },
  }, new Map([[pageId, original]]));
  return { result, reviewed, correction };
}

it.each([
  [{ ...support, quote: "建议保留全部不存在的审计日志。" }],
  [support, support],
])("Given invalid supporting references %j, When corrected before review, Then valid support reaches independent review", async (...initialSupport) => {
  const { result, reviewed, correction } = await correctSupportingReferences(initialSupport);
  expect(correction?.reason).toContain("invalid_supporting_quote");
  expect(correction?.review).toBeUndefined();
  expect(result.status).toBe("submitted");
  expect(reviewed).toHaveLength(1);
  expect(reviewed[0][0].supportingQuotes).toEqual([support]);
});

it("Given accepted supporting terms and a page-level rejection, When correction drops support, Then the final review retains those terms", async () => {
  const { result, reviewed } = await correctSupportingReferences([support], true);
  expect(result.status).toBe("submitted");
  expect(reviewed).toHaveLength(2);
  expect(reviewed[1][0].supportingQuotes).toEqual([support]);
});
