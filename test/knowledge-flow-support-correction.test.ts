/** Quote-bound support selectors reject unknown sources and normalize duplicates before review. */
import { expect, it } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowClaim } from "../extensions/knowledge-flow/types.js";
import { accepted, draft, job, original, pageId, plan, runConsolidation } from "./knowledge-flow-consolidation-fixtures.js";

const proposal = "建议将样例预算调整为28个虚构单位，并保留复核日志。";
const support = { evidenceId: "proposal", quote: proposal };

function jobWithProposal() {
  const input = job();
  input.evidence.push({ id: support.evidenceId, kind: "assistant", text: proposal, sha256: sha256Text(proposal),
    observedAt: input.createdAt, locator: "synthetic:proposal" });
  return input;
}

it("Given a support selector outside frozen evidence, When the editor returns it, Then it is rejected before review", async () => {
  const input = jobWithProposal();
  const initial = draft();
  initial.claims[0].supportingQuotes = [{ ...support, quote: "建议保留全部不存在的审计日志。" }];
  let reviews = 0;

  await expect(runConsolidation(input, { knowledge_topic_plan: plan(), knowledge_topic_edit: initial,
    knowledge_topic_review: () => { reviews += 1; return accepted(); } }, new Map([[pageId, original]])))
    .rejects.toThrow(/supportingQuotes\/0\/quoteId must be equal to one of the allowed values/);

  expect(reviews).toBe(0);
});

it("Given repeated selectors for one frozen support quote, When the draft resolves, Then review sees the evidence once", async () => {
  const input = jobWithProposal();
  const initial = draft();
  initial.claims[0].supportingQuotes = [support, support];
  const reviewed: FlowClaim[][] = [];
  const result = await runConsolidation(input, { knowledge_topic_plan: plan(), knowledge_topic_edit: initial,
    knowledge_topic_review: (request: { claims: FlowClaim[] }) => {
      reviewed.push(request.claims);
      return accepted();
    } }, new Map([[pageId, original]]));

  expect(result.status).toBe("submitted");
  expect(reviewed).toHaveLength(1);
  expect(reviewed[0][0].supportingQuotes).toEqual([support]);
});

it("Given accepted supporting terms and a page-level rejection, When prose is corrected, Then support stays locked across fresh review", async () => {
  const input = jobWithProposal();
  const initial = draft();
  initial.claims[0].supportingQuotes = [support];
  const corrected = structuredClone(initial);
  corrected.pages[0].body = "## 当前结论\n按已确认的样例预算执行。{{claim:0}}\n\n## 历史\n预算12个虚构单位。^[old.md:1]";
  const reviewed: FlowClaim[][] = [];
  let reviewCount = 0;
  const result = await runConsolidation(input, { knowledge_topic_plan: plan(), knowledge_topic_edit: (request: { correction?: unknown }) =>
    request.correction ? corrected : initial,
    knowledge_topic_review: (request: { claims: FlowClaim[] }) => {
      reviewed.push(request.claims);
      reviewCount += 1;
      return { ...accepted(), decision: reviewCount === 1 ? "reject" : "accept",
        ...(reviewCount === 1 ? { claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "主张的两条来源均保留" }] } : {}) };
    } }, new Map([[pageId, original]]));

  expect(result.status).toBe("submitted");
  expect(reviewed).toHaveLength(2);
  expect(reviewed[1][0].supportingQuotes).toEqual([support]);
});
