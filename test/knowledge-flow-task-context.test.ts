/** Task scope is carried through every curation stage without becoming evidence. */
import { describe, expect, it, onTestFinished } from "vitest";
import Ajv from "ajv";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { buildFrontmatter } from "../src/utils/markdown.js";
import { sha256Text } from "../src/connectors/hash.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { createQuoteBoundEditTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { FlowConfig, FlowEvidence, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { LLMProvider } from "../src/utils/provider.js";
import { quoteBoundDraftFromCatalog } from "./knowledge-flow-consolidation-fixtures.js";

const PAGE = "concepts/sample-creative-test";
const TASK = "只更新样例素材实验的模拟预算约束，不扩展到其他项目。";
const SOURCE = "用户确认样例素材实验预算从12个虚构单位调整到28个虚构单位。";
const TOPIC_ID = sha256Text("sample-creative-test");
const ORIGINAL = `${buildFrontmatter({ title: "样例素材实验", projectId: "companion", knowledgeTopicId: TOPIC_ID,
  knowledgeTopic: "样例素材实验", knowledgeDecisionObject: "素材预算" })}\n\n当前模拟预算12个虚构单位。^[old.md:1]\n`;

interface PromptRequest { currentTaskContext?: string; originalEvidence?: FlowEvidence[]; evidence?: unknown; correction?: Record<string, unknown>;
  claims?: Array<{ kind: string; status: string }> }
interface Call { tool: string; system: string; request: PromptRequest }

function evidence(kind: FlowEvidence["kind"] = "user", text = SOURCE): FlowEvidence {
  return { id: "e1", kind, text, locator: "turn:e1", observedAt: "2026-09-21T00:00:00Z", sha256: sha256Text(text) };
}

function job(item: FlowEvidence = evidence()): FlowJob {
  return { id: "task-context-contract", projectId: "companion", projectLabel: "Companion", sessionId: "s", turnId: "t",
    cwd: "/tmp/project", createdAt: "2026-09-21T00:00:00Z", prompt: TASK, lastAssistant: "", allowedPageIds: [PAGE],
    evidence: [item], sessionContext: { version: 1, revision: 1, summary: "样例素材实验预算讨论", topicPageIds: [PAGE], evidence: [] } };
}

function plan() {
  return { summary: "更新样例素材实验预算", disposition: "edit", reason: "同一决策页", pages: [{ action: "update",
    targetPageId: PAGE, title: "样例素材实验", topic: "样例素材实验", decisionObject: "素材预算", reason: "复用既有页" }] };
}

function draft(overrides: Partial<FlowEvidence> = {}, claimOverrides: Record<string, unknown> = {}): TopicDraft {
  const source = evidence(overrides.kind, overrides.text);
  return { claims: [{ text: source.text, evidenceId: source.id, quote: source.text, title: "素材预算", topic: "样例素材实验",
    decisionObject: "素材预算", slug: "sample-creative-test", targetPageId: PAGE, kind: "decision", status: "decided",
    useWhen: "执行素材预算调整时", rationale: "用户明确模拟预算变化", replacementIntent: false, supportingQuotes: [], ...claimOverrides }],
    pages: [{ pageId: PAGE, body: "## 当前结论\n预算调整。{{claim:0}}\n\n## 历史\n预算12个虚构单位。^[old.md:1]", claimIndexes: [0] }],
    summary: "保留预算决策和历史理由。" };
}

function config(item: FlowEvidence = evidence()): { config: FlowConfig; calls: Call[] } {
  const stateDir = mkdtempSync(path.join(tmpdir(), "task-context-state-"));
  mkdirSync(path.join(stateDir, "wiki/sources"), { recursive: true });
  writeFileSync(path.join(stateDir, "wiki/sources/old.md"), "用户确认历史预算与适用范围。\n");
  onTestFinished(() => rm(stateDir, { recursive: true, force: true }));
  const calls: Call[] = [];
  let reviewRejected = false;
  const catalog = buildCorrectionEvidence([item]);
  const quoteId = catalog[0].quoteOptions[0].quoteId;
  const unsupported = async (): Promise<never> => { throw new Error("unexpected provider operation"); };
  const provider: LLMProvider = { complete: unsupported, stream: unsupported, embed: unsupported,
    toolCall: async (system, messages, tools) => {
      const tool = tools[0].name; const request = JSON.parse(messages[0].content) as PromptRequest;
      calls.push({ tool, system, request });
      if (tool === "knowledge_topic_plan") return JSON.stringify(plan());
      if (tool === "knowledge_topic_edit") {
        if (request.correction) return JSON.stringify({ claimUpdates: [], droppedClaimIds: [],
          pages: [{ pageId: PAGE, body: "## 当前结论\n预算调整。{{claim:c0}}\n\n## 历史\n预算12个虚构单位。^[old.md:1]", claimIds: ["c0"] }],
          summary: "保留预算决策和历史理由。" });
        return JSON.stringify(quoteBoundDraftFromCatalog(draft(), catalog));
      }
      if (tool === "knowledge_topic_review") {
        if (!reviewRejected) { reviewRejected = true; return JSON.stringify({ decision: "reject", reason: "请修正叙事", checkedClaimIndexes: [0], checkedPageIds: [PAGE] }); }
        return JSON.stringify({ decision: "accept", reason: "完整", checkedClaimIndexes: [0], checkedPageIds: [PAGE] });
      }
      throw new Error(`unexpected tool ${tool}`);
    } };
  return { calls, config: { wikiRoot: path.join(stateDir, "wiki"), stateDir, model: "test", maxProposals: 5,
    maxPendingPerProject: 10, provider, reviewer: provider, machineId: "test", exchange: { root: "/tmp/exchange", participants: ["test"] } } };
}

describe("knowledge-flow current task context contract", () => {
  it("Given a bounded task, When editor and reviewer correct it, Then every stage receives the exact non-citable scope", async () => {
    const harness = config();
    const result = await consolidateSession(job(), harness.config, new Map([[PAGE, ORIGINAL]]));
    expect(result.status).toBe("submitted");
    const curation = harness.calls.filter(call => ["knowledge_topic_edit", "knowledge_topic_review"].includes(call.tool));
    expect(curation.length).toBeGreaterThan(0);
    const planning = harness.calls.find(call => call.tool === "knowledge_topic_plan")!;
    expect(planning.request.currentTaskContext).toBe(TASK);
    // Planning retains full source records; every edit and review uses the lossless quote catalog.
    const tagged = [{ ...evidence(), origin: "current" as const }];
    const expectedCatalog = buildCorrectionEvidence(tagged);
    expect(planning.request.originalEvidence).toEqual(tagged);
    for (const call of curation) expect(call.request.currentTaskContext).toBe(TASK);
    expect(harness.calls.some(call => call.tool === "knowledge_topic_edit" && call.request.correction)).toBe(true);
    const initialEdits = harness.calls.filter(call => call.tool === "knowledge_topic_edit" && !call.request.correction);
    const reviews = harness.calls.filter(call => call.tool === "knowledge_topic_review");
    expect(initialEdits.every(call => JSON.stringify(call.request.evidence) === JSON.stringify(expectedCatalog))).toBe(true);
    const correctionEdits = harness.calls.filter(call => call.tool === "knowledge_topic_edit" && call.request.correction);
    expect(reviews.every(call => JSON.stringify(call.request.evidence) === JSON.stringify(expectedCatalog))).toBe(true);
    expect(correctionEdits.every(call => JSON.stringify(call.request.evidence) === JSON.stringify(expectedCatalog))).toBe(true);
    for (const call of harness.calls) {
      if (["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_review"].includes(call.tool)) {
        expect(call.system).toContain("scope-only");
        expect(call.system).toContain("cannot be cited as evidence");
        expect(call.system).toContain("cannot establish user approval");
        expect(call.system).toContain("cannot override evidence, policy, role, or routing rules");
      }
    }
  });

  it("Given a task scope or non-user source, When a claim is proposed, Then quote and role schemas reject promotion", () => {
    const candidate = (item: FlowEvidence, kind: string, status: string, quoteId?: string) => {
      const catalog = buildCorrectionEvidence([item]);
      const option = catalog[0].quoteOptions[0];
      const claim = { text: item.text, title: "素材预算", slug: "sample-creative-test", targetPageId: PAGE,
        kind, status, useWhen: "执行素材预算调整时", rationale: "来源文本", replacementIntent: false,
        quoteId: quoteId ?? option.quoteId, supportingQuotes: [] };
      const validate = new Ajv({ allErrors: true, strict: false })
        .compile(createQuoteBoundEditTool([PAGE], catalog).input_schema);
      return { validate, output: { claims: [claim], pages: [{ pageId: PAGE, body: "{{claim:0}}", claimIndexes: [0] }], summary: "保留来源" } };
    };

    const user = evidence();
    const userClaim = candidate(user, "decision", "decided");
    expect(userClaim.validate(userClaim.output)).toBe(true);
    const taskOnly = candidate(user, "decision", "decided", "task-context-is-not-evidence");
    expect(taskOnly.validate(taskOnly.output)).toBe(false);

    const artifact = evidence("artifact");
    const artifactFact = candidate(artifact, "fact", "historical");
    expect(artifactFact.validate(artifactFact.output)).toBe(true);
    const artifactDecision = candidate(artifact, "decision", "historical");
    expect(artifactDecision.validate(artifactDecision.output)).toBe(false);
    const assistant = evidence("assistant");
    const assistantLesson = candidate(assistant, "lesson", "historical");
    expect(assistantLesson.validate(assistantLesson.output)).toBe(true);
    const assistantDecision = candidate(assistant, "decision", "historical");
    expect(assistantDecision.validate(assistantDecision.output)).toBe(false);
  });
});
