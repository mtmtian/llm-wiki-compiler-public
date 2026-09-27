/**
 * Phase A of deployment/KNOWLEDGE-LEDGER.md: deterministic fixes that keep one mislabeled claim or a one-off page name
 * from holding a whole session batch.
 */
import { describe, expect, it } from "vitest";
import { validatedDraft, withRoleAuthority } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { resolvePlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import type { PlannedPage, TopicPlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowClaim, FlowEvidence, FlowJob } from "../extensions/knowledge-flow/types.js";

const PAGE: PlannedPage = { pageId: "concepts/growth-rules", topicId: "topic-id", title: "投放规则",
  topic: "投放结构", decisionObject: "投放预算规则", basisHash: null, original: null };

function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, locator: `turn:${id}`, observedAt: "2026-09-26T00:00:00Z", sha256: sha256Text(text) };
}

const USER = evidence("user-1", "user", "以后加词都补进原系列，不要另建系列");
const ASSISTANT = evidence("assistant-1", "assistant", "已把三个精确匹配词补入原系列并启用");
const ARTIFACT = evidence("artifact-1", "artifact", "参考文档：每个关键词单独建系列");

function claim(source: FlowEvidence, overrides: Partial<FlowClaim> = {}): FlowClaim {
  return { text: source.text, evidenceId: source.id, quote: source.text, title: PAGE.title, topic: PAGE.topic,
    decisionObject: PAGE.decisionObject, slug: "growth-rules", targetPageId: PAGE.pageId, kind: "decision",
    status: "decided", useWhen: "调整投放结构时", rationale: "来源原话", replacementIntent: false,
    supportingQuotes: [], ...overrides };
}

function draft(claims: FlowClaim[]): TopicDraft {
  const markers = claims.map((_, index) => `{{claim:${index}}}`).join("\n");
  return { summary: "投放规则", claims, pages: [{ pageId: PAGE.pageId, body: `## 当前结论\n${markers}`,
    claimIndexes: claims.map((_, index) => index) }] };
}

const JOB = { evidence: [USER, ASSISTANT, ARTIFACT], allowedPageIds: [PAGE.pageId] } as FlowJob;

describe("role authority normalization", () => {
  it("Given an assistant-primary decision, When normalized, Then the page validates as a historical lesson", () => {
    const original = draft([claim(ASSISTANT)]);
    expect(() => validatedDraft(original, JOB, [PAGE], 5)).toThrow(/assistant_authority/);
    const [result] = validatedDraft(withRoleAuthority(original, JOB.evidence), JOB, [PAGE], 5).claims;
    expect(result).toMatchObject({ kind: "lesson", status: "historical", text: ASSISTANT.text, quote: ASSISTANT.text });
  });

  it("Given an artifact-primary decision, When normalized, Then it becomes a historical fact instead of holding the page", () => {
    const original = draft([claim(ARTIFACT)]);
    expect(() => validatedDraft(original, JOB, [PAGE], 5)).toThrow(/not evidence of a user decision/);
    const [result] = validatedDraft(withRoleAuthority(original, JOB.evidence), JOB, [PAGE], 5).claims;
    expect(result).toMatchObject({ kind: "fact", status: "historical" });
  });

  it("Given assistant supporting quotes, When normalized, Then only a user-primary claim keeps them", () => {
    const support = [{ evidenceId: ASSISTANT.id, quote: ASSISTANT.text }];
    const normalized = withRoleAuthority(draft([claim(USER, { supportingQuotes: support }),
      claim(ARTIFACT, { kind: "constraint", status: "historical", supportingQuotes: support })]), JOB.evidence);
    expect(normalized.claims.map(item => item.supportingQuotes?.length)).toEqual([1, 0]);
    expect(() => validatedDraft(normalized, JOB, [PAGE], 5)).not.toThrow();
  });

  it("Given a claim the model marked uncertain, When normalized, Then it still holds for review", () => {
    const original = draft([claim(ASSISTANT, { status: "uncertain" })]);
    expect(withRoleAuthority(original, JOB.evidence).claims[0]).toEqual(original.claims[0]);
  });
});

describe("durable page names", () => {
  const job = { projectId: "growth", projectLabel: "Growth", evidence: [USER], allowedPageIds: [] } as unknown as FlowJob;
  const plan = (title: string, decisionObject = "投放预算规则"): TopicPlan => ({ summary: "投放", disposition: "edit",
    reason: "新主题", pages: [{ action: "create", targetPageId: null, title, topic: "投放结构", decisionObject, reason: "独立对象" }] });

  it("Given a new page named after one dated action or batch, When planned, Then the plan is rejected for correction", () => {
    expect(() => resolvePlan(plan("2026-09-24 Search04 投放调整"), job, new Map())).toThrow(/timeline section/);
    expect(() => resolvePlan(plan("视频素材追加", "2025-01-15 样例批次素材"), job, new Map())).toThrow(/timeline section/);
  });

  it("Given a durable topic name, When planned, Then a new page is still allowed", () => {
    expect(resolvePlan(plan("投放结构与预算规则"), job, new Map())).toHaveLength(1);
    expect(resolvePlan(plan("2026 年度预算规划"), job, new Map())).toHaveLength(1);
  });
});
