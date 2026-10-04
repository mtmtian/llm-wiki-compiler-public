/** Full-page no-change outcomes must preserve originals and survive independent semantic review. */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { accepted, config, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

/** The source is already represented, so the editor keeps every original paragraph. */
function unchanged(): TopicDraft {
  return { claims: [], pages: [{ pageId, body: "{{keep:P1}}", claimIndexes: [] }], summary: "Existing knowledge already covers this source." };
}

/** Run the production consolidation boundary with independently scripted edit and review stages. */
async function consolidate(changed: TopicDraft, review = { ...accepted(), checkedClaimIndexes: [] }, topicPlan = plan()) {
  const calls: string[] = [];
  const runtime = config({ knowledge_topic_plan: topicPlan, knowledge_topic_edit: changed, knowledge_topic_review: review },
    (_system, name) => calls.push(name));
  const existing = new Map([[pageId, original]]);
  const result = await consolidateSession(job(), runtime, existing);
  expect(existing.get(pageId)).toBe(original);
  expect(result.contribution).toBeUndefined();
  expect(result.ledgerContribution).toBeUndefined();
  return { result, calls };
}

describe("independently reviewed unchanged topic drafts", () => {
  it("Given an unchanged draft, When independent review accepts no durable addition, Then exits empty after one edit and review", async () => {
    const { result, calls } = await consolidate(unchanged());
    expect(result.status).toBe("empty");
    expect(result.reviewCount).toBe(0);
    expect(calls).toEqual(["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review"]);
  });

  it.each(["reject", "needs_review"])("Given omitted knowledge detected by review (%s), Then never silently drops the batch", async decision => {
    const { result, calls } = await consolidate(unchanged(), { ...accepted(), checkedClaimIndexes: [], decision, reason: "Required knowledge was omitted." });
    expect(result).toMatchObject({ status: "needs_review", error: "Required knowledge was omitted." });
    expect(calls).toContain("knowledge_topic_review");
  });

  it("Given acceptance with missing page coverage, Then holds the unchanged draft", async () => {
    const { result } = await consolidate(unchanged(), { ...accepted(), checkedClaimIndexes: [], checkedPageIds: [] });
    expect(result).toMatchObject({ status: "needs_review", error: expect.stringContaining("page coverage mismatch") });
  });

  it.each(["body", "retirement", "indexes", "duplicate", "missing"])("Given an invalid empty-claim draft (%s), Then cannot use the no-change exit", async defect => {
    const changed = unchanged();
    if (defect === "body") changed.pages[0].body = "Unsupported new conclusion.";
    if (defect === "retirement") changed.pages[0].citationRetirements = [{ citation: "^[old.md:1]", reason: "remove", replacement: "^[old.md:1]" }];
    if (defect === "indexes") changed.pages[0].claimIndexes = [0];
    if (defect === "duplicate") changed.pages.push(changed.pages[0]);
    if (defect === "missing") changed.pages = [];
    const { result, calls } = await consolidate(changed);
    expect(result.status).toBe("needs_review");
    expect(calls).not.toContain("knowledge_topic_review");
  });

  it("Given a planned label change without claims, Then cannot silently discard that change", async () => {
    const changedPlan = plan(); changedPlan.pages[0].title = "Changed title";
    const { result, calls } = await consolidate(unchanged(), undefined, changedPlan);
    expect(result.status).toBe("needs_review");
    expect(calls).not.toContain("knowledge_topic_review");
  });

  it("Given a new destination without claims, Then no-change acceptance is unavailable", async () => {
    const runtime = config({ knowledge_topic_plan: { ...plan(), pages: [{ ...plan().pages[0], action: "create", targetPageId: null }] },
      knowledge_topic_edit: { ...unchanged(), pages: [] } });
    const input = job(); input.allowedPageIds = [];
    const result = await consolidateSession(input, runtime, new Map());
    expect(result.status).toBe("needs_review");
    expect(result.contribution).toBeUndefined();
  });
});
