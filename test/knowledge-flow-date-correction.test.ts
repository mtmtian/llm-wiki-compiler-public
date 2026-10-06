/** Metadata-derived dates must be repaired before model review without changing source bindings. */
import { expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

it("Given a capture date written as report date, When consolidating, Then correction preserves its quote and review sees only the repaired prose", async () => {
  const wrong = draft();
  wrong.pages[0].body = wrong.pages[0].body.replace("预算 28", "报告于 2025-01-15：预算 28");
  const requests: any[] = []; const reviews: any[] = [];
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
    requests.push(request); return request.correction ? draft() : wrong;
  }, knowledge_topic_review: (request: any) => { reviews.push(request); return accepted(); } });

  const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));

  expect(result.status).toBe("submitted");
  expect(requests).toHaveLength(2);
  expect(requests[1].correction.diagnostics).toMatch(/capture or publication metadata date/);
  expect(requests[1].correction.permissions.replaceEvidenceForClaimIds).toEqual([]);
  expect(reviews).toHaveLength(1);
  expect(reviews[0].revisions[0].body).not.toContain("报告于 2025-01-15");
  expect(result.contribution?.claims[0].quote).toBe(draft().claims[0].quote);
});
