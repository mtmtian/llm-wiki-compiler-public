/**
 * Regression coverage for page-length checks after existing keep placeholders expand.
 * The fixture drives the real consolidation stages with a scripted provider and an existing
 * cited page near the production limit, so a correction must receive actionable space data,
 * compact redundant prose, and carry the old citation into the reviewed revision.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { MAX_TOPIC_BODY_CHARS, resolvePlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { validatedDraft, type TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { withKeptParagraphs } from "../extensions/knowledge-flow/kept-paragraphs.js";
import { buildFrontmatter, parseFrontmatter } from "../src/utils/markdown.js";
import { accepted, config, draft, job, pageId, plan, topicId } from "./knowledge-flow-consolidation-fixtures.js";

const OLD_CITATION = "^[old.md:1]";
const HISTORY_HEADING = "## 决策历史\n";

function repeatedTextOfLength(value: string, length: number): string {
  const count = Math.floor(length / value.length);
  return value.repeat(count) + "旧".repeat(length - count * value.length);
}

function nearLimitOriginal(): string {
  const bodyLength = MAX_TOPIC_BODY_CHARS - 20;
  const historyLength = bodyLength - HISTORY_HEADING.length - OLD_CITATION.length;
  const metadata = { title: "样例素材测试预算", projectId: "companion", knowledgeTopicId: topicId,
    knowledgeTopic: "样例素材推广", knowledgeDecisionObject: "样例素材测试预算" };
  return `${buildFrontmatter(metadata)}\n\n${HISTORY_HEADING}${repeatedTextOfLength("此前模拟预算为12个虚构单位。", historyLength)}${OLD_CITATION}`;
}

function overlongDraft(): TopicDraft {
  const value = draft();
  value.pages[0].body = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n{{keep:P1}}";
  return value;
}

function correctedPageBody(): string {
  return "## 当前结论\n预算 28 个虚构单位。{{claim:c0}}\n\n## 决策历史\n旧模拟预算为12个虚构单位。^[old.md:1]";
}

function correctionPatch() {
  return { claimUpdates: [], droppedClaimIds: [], pages: [{ pageId, claimIds: ["c0"], body: correctedPageBody() }],
    summary: "压缩重复历史说明并保留原引文。" };
}

function expectedOverflowDiagnostic(bodyChars: number): string {
  const excess = bodyChars - MAX_TOPIC_BODY_CHARS;
  return `topic page ${pageId} body exceeds limit after keep expansion: ${bodyChars}/${MAX_TOPIC_BODY_CHARS} characters (${excess} over); `
    + "compress repeated prose on this page while preserving supported information and existing citations.";
}

function expandedDraftAndLength(existing: string) {
  const input = job();
  const pages = resolvePlan(plan(), input, new Map([[pageId, existing]]));
  const expanded = withKeptParagraphs(overlongDraft(), pages);
  return { input, pages, expanded, bodyChars: parseFrontmatter(expanded.pages[0].body).body.length };
}

function expectBudgetRequests(requests: Array<Record<string, any>>, original: string, systems: string[]): void {
  const bodyChars = parseFrontmatter(original).body.length;
  for (const request of requests) {
    const budget = request.pageBodyBudgets?.find((item: Record<string, any>) => item.pageId === pageId);
    expect(budget).toEqual({ pageId, currentBodyChars: bodyChars, maximumBodyChars: MAX_TOPIC_BODY_CHARS,
      additionalAvailableChars: MAX_TOPIC_BODY_CHARS - bodyChars });
    expect(request.pages[0].originalParagraphs[0].keep.length).toBeLessThan(bodyChars);
  }
  expect(systems.every(system => system.toLowerCase().includes("keep placeholders count as their full restored paragraphs"))).toBe(true);
}

describe("consolidation page body budget", () => {
  it("Given a near-limit source page, When keep plus new prose expands past the cap, Then validation reports the page and exact excess", () => {
    const existing = nearLimitOriginal();
    const { input, pages, expanded, bodyChars } = expandedDraftAndLength(existing);
    expect(parseFrontmatter(existing).body.length).toBeGreaterThan(MAX_TOPIC_BODY_CHARS - 100);
    expect(bodyChars).toBeGreaterThan(MAX_TOPIC_BODY_CHARS);

    expect(() => validatedDraft(expanded, input, pages, 5)).toThrow(expectedOverflowDiagnostic(bodyChars));
  });

  it("Given a keep-expanded draft over the cap, When the editor receives its correction, Then a compressed citation-preserving revision is accepted", async () => {
    const original = nearLimitOriginal();
    const existing = new Map([[pageId, original]]);
    const requests: Array<Record<string, any>> = [];
    const systems: string[] = [];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: Record<string, any>) => {
      requests.push(request);
      return request.correction ? correctionPatch() : overlongDraft();
    }, knowledge_topic_review: accepted() }, (system, tool) => {
      if (tool === "knowledge_topic_edit") systems.push(system);
    });
    const { bodyChars } = expandedDraftAndLength(original);

    const result = await consolidateSession(job(), runtime, existing);

    expect(result.status).toBe("submitted");
    expectBudgetRequests(requests, original, systems);
    expect(requests[1].correction.diagnostics).toBe(expectedOverflowDiagnostic(bodyChars));
    const revision = result.contribution?.topicRevisions?.[0];
    expect(revision?.body.length).toBeLessThanOrEqual(MAX_TOPIC_BODY_CHARS);
    expect(revision?.body).toContain(OLD_CITATION);
  });
});
