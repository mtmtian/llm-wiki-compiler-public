/** Capacity recovery exercises the complete planner/editor/reviewer loop with real paragraph expansion. */
import { expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { MAX_TOPIC_BODY_CHARS } from "../extensions/knowledge-flow/consolidation-plan.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

/** One preserved paragraph leaves too little room for the new evidence. */
function fullPage(): string {
  return original + "x".repeat(MAX_TOPIC_BODY_CHARS - parseFrontmatter(original).body.length - 10);
}

/** New destinations contain new evidence alone; updates preserve both original paragraphs. */
function edit(request: Record<string, any>) {
  const next = draft();
  const destination = request.pages[0].pageId;
  next.claims[0].targetPageId = destination;
  next.pages = [{ pageId: destination, claimIndexes: [0], body: request.pages[0].original === null
    ? "预算调整为 28 个虚构单位。{{claim:0}}"
    : "{{keep:P1}}\n\n{{keep:P2}}\n\n预算调整为 28 个虚构单位。{{claim:0}}" }];
  return next;
}

function separatePlan() {
  const next = plan();
  next.pages = [{ ...next.pages[0], action: "create", targetPageId: null,
    title: "预算调整规则", decisionObject: "预算调整规则", reason: "现有预算页容量不足，单独记录预算调整规则" }];
  return next;
}

/** Use the same frozen full page for original execution and durable recovery. */
function consolidateCapacity(runtime: ReturnType<typeof config>) {
  return consolidateSession(job(), runtime, new Map([[pageId, fullPage()]]));
}

/** A reviewer covers the actual selected destinations, including newly planned sub-workstreams. */
function reviewSelected(request: Record<string, any>) {
  return { ...accepted(), checkedPageIds: request.revisions.map((page: any) => page.pageId) };
}

it("Given expanded keeps still exceed capacity after compression, When a distinct destination is planned, Then the same batch succeeds without rewriting the full page", async () => {
  const plans: any[] = []; const edits: any[] = [];
  const runtime = config({ knowledge_topic_plan: (request: any) => {
    plans.push(request); return plans.length === 1 ? plan() : separatePlan();
  }, knowledge_topic_edit: (request: any) => { edits.push(request); return edit(request); },
  knowledge_topic_review: reviewSelected });

  const result = await consolidateCapacity(runtime);

  expect(result.status).toBe("submitted");
  expect(plans).toHaveLength(2);
  expect(plans[1].correction.capacity).toMatchObject({ pageId, maxBodyChars: MAX_TOPIC_BODY_CHARS });
  expect(plans[1].correction.capacity.bodyChars).toBeGreaterThan(MAX_TOPIC_BODY_CHARS);
  expect(edits[0].pageBodyBudgets[0]).toMatchObject({ maximumBodyChars: MAX_TOPIC_BODY_CHARS, additionalAvailableChars: 10 });
  expect(edits[1].correction.diagnostics).toMatch(/body exceeds limit after keep expansion/);
  expect(edits[1].correction.permissions.replaceEvidenceForClaimIds).toEqual([]);
  expect(result.contribution?.topicRevisions?.[0].pageId).not.toBe(pageId);
  expect(edits).toHaveLength(3);
  const replay = await consolidateCapacity(runtime);
  expect(replay).toEqual(result);
  expect(plans).toHaveLength(2);
  expect(edits).toHaveLength(3);
});

it("Given replanning chooses another overflowing edit, Then recovery stops as a technical failure after one replan", async () => {
  let plans = 0; let edits = 0;
  const runtime = config({ knowledge_topic_plan: () => { plans += 1; return plan(); },
    knowledge_topic_edit: (request: any) => { edits += 1; return edit(request); } });

  const result = await consolidateCapacity(runtime);

  expect(result).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
  expect(result.error).toMatch(/body.*exceeds.*12000/);
  expect(plans).toBe(2);
  expect(edits).toBe(3);
});

it("Given raw model prose alone exceeds the schema limit, Then it uses the same bounded capacity recovery", async () => {
  let plans = 0;
  const runtime = config({ knowledge_topic_plan: () => ++plans === 1 ? plan() : separatePlan(),
    knowledge_topic_edit: (request: any) => {
      const next = edit(request);
      if (plans === 1) next.pages[0].body = "x".repeat(MAX_TOPIC_BODY_CHARS + 1);
      return next;
    }, knowledge_topic_review: reviewSelected });

  const result = await consolidateCapacity(runtime);

  expect(result.status).toBe("submitted");
  expect(plans).toBe(2);
});

it("Given a raw overflow followed by a transport interruption, When retrying the batch, Then it reuses the original overflow and capacity plan", async () => {
  let plans = 0; let initialEdits = 0; let recoveryEdits = 0;
  const runtime = config({ knowledge_topic_plan: () => ++plans === 1 ? plan() : separatePlan(),
    knowledge_topic_edit: (request: any) => {
      const next = edit(request);
      if (request.pages[0].pageId === pageId) next.pages[0].body = "x".repeat(MAX_TOPIC_BODY_CHARS + ++initialEdits);
      else if (++recoveryEdits === 1) throw new Error("temporary model transport interruption");
      return next;
    }, knowledge_topic_review: reviewSelected });

  const interrupted = await consolidateCapacity(runtime);
  expect(interrupted.status).toBe("error");
  expect(interrupted.retryable).not.toBe(false);
  expect(interrupted.error).toContain("temporary model transport interruption");
  const result = await consolidateCapacity(runtime);
  expect(result.status).toBe("submitted");
  expect(await consolidateCapacity(runtime)).toEqual(result);
  expect({ plans, initialEdits, recoveryEdits }).toEqual({ plans: 2, initialEdits: 1, recoveryEdits: 2 });
});

it("Given a replanned draft needs a reviewer correction, Then its review history preserves the correction stage", async () => {
  let plans = 0; let reviews = 0;
  const runtime = config({ knowledge_topic_plan: () => ++plans === 1 ? plan() : separatePlan(),
    knowledge_topic_edit: edit, knowledge_topic_review: (request: any) => ++reviews === 1
      ? { ...reviewSelected(request), decision: "reject", reason: "请确认适用范围", claimDecisions: [
        { claimIndex: 0, decision: "reject", reason: "请明确适用范围" }] }
      : reviewSelected(request) });

  const result = await consolidateCapacity(runtime);

  expect(result.status).toBe("submitted");
  expect(result.claimReviews?.map(review => review.stage)).toEqual(["initial", "correction"]);
});
