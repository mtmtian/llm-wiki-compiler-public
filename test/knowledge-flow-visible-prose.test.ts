/** A valid claim and citation cannot substitute for readable knowledge in the rendered page. */
import { expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { renderPublicationSources, renderTopicRevisionPage } from "../extensions/knowledge-flow/materialize-render.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowResult } from "../extensions/knowledge-flow/types.js";
import { accepted, config, draft, expectNoReadablePage, expectNoReview, job, original, pageId, plan,
  runConsolidationWithCalls } from "./knowledge-flow-consolidation-fixtures.js";

const citedHistory = "此前预算 12 个虚构单位。^[old.md:1]";

it.each(["{{claim:0}}", "## 当前结论\n{{claim:0}}", "- {{claim:0}}", "1. {{claim:0}}",
  "{{claim:0}} ^[old.md:1]"])("Given only citation syntax after an edit, Then it cannot become a readable page: %s", async body => {
  const edited = draft();
  edited.pages[0].body = `${body}\n\n${citedHistory}`;
  const { result, calls } = await runConsolidationWithCalls(job(),
    { knowledge_topic_plan: plan(), knowledge_topic_edit: edited, knowledge_topic_review: accepted() }, new Map([[pageId, original]]));
  expectNoReadablePage(result);
  expectNoReview(calls);
});

it("Given missing page prose, When the bounded correction writes it, Then the rendered page contains knowledge and citations", async () => {
  const initial = draft();
  initial.pages[0].body = `## 当前结论\n{{claim:0}}\n\n${citedHistory}`;
  const calls: string[] = [];
  const runtime = config({ knowledge_topic_plan: plan(),
    knowledge_topic_edit: (request: { correction?: unknown }) => request.correction ? draft() : initial,
    knowledge_topic_review: accepted() }, (_system, name) => calls.push(name));
  const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  expect(calls.filter(name => name === "knowledge_topic_edit")).toHaveLength(2);
  expect(calls.filter(name => name === "knowledge_topic_review")).toHaveLength(1);
  const page = renderedPage(result);
  expect(page).toContain("预算 28 个虚构单位。");
  expect(page).toContain("^[");
  expect(page).not.toContain("{{claim:");
});

it("Given an accepted claim, When a page correction omits its prose, Then no third edit or publication occurs", async () => {
  const { result, calls } = await runConsolidationWithCalls(job(), { knowledge_topic_plan: plan(),
    knowledge_topic_edit: (request: { correction?: unknown }) => request.correction
      ? { claimUpdates: [], droppedClaimIds: [], pages: [{ pageId, claimIds: ["c0"],
        body: `## 当前结论\n{{claim:c0}}\n\n${citedHistory}` }], summary: "保留已接受的主张。" } : draft(),
    knowledge_topic_review: { ...accepted(), decision: "reject", reason: "修正页面标题。",
      claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "原文直接支持主张。" }] } }, new Map([[pageId, original]]));
  expectNoReadablePage(result);
  expect(calls.filter(name => name === "knowledge_topic_edit")).toHaveLength(2);
  expect(calls.filter(name => name === "knowledge_topic_review")).toHaveLength(1);
});

/** Exercise the existing deterministic renderer; a claim placeholder inserts a citation only. */
function renderedPage(result: FlowResult): string {
  if (!result.contribution?.topicRevisions?.[0]) throw new Error("expected reviewed page contribution");
  const record: PublicationRecord = { id: "a".repeat(64), payload: {
    version: 2, baselineId: "b".repeat(64), machineId: "test", projectId: "companion", projectLabel: "Companion",
    createdAt: job().createdAt, originJobHash: "c".repeat(64), repoIdentity: null, basisRecordIds: [],
    ...result.contribution, review: { status: "accepted", model: "scripted" },
  } };
  const entries = record.payload.claims.map((claim, index) => ({ record, claim, index, ref: `${record.id}:${index}` }));
  const sources = renderPublicationSources([], entries);
  return renderTopicRevisionPage(result.contribution.topicRevisions[0], record, original, sources.get(record.id)!, sources);
}
