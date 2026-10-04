/** Shared session-consolidation fixtures: one budget page, a scripted model and an accepting review. */
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { onTestFinished } from "vitest";
import type { TopicPlan } from "../extensions/knowledge-flow/consolidation-plan.js";
import { sha256Text } from "../src/connectors/hash.js";
import { buildFrontmatter } from "../src/utils/markdown.js";
import type { FlowConfig, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { FlowResult } from "../extensions/knowledge-flow/types.js";

export const pageId = "concepts/sample-creative-budget";
export const topicId = sha256Text("budget");
export const original = `${buildFrontmatter({ title: "样例素材测试预算", projectId: "companion", knowledgeTopicId: topicId,
  knowledgeTopic: "样例素材推广", knowledgeDecisionObject: "样例素材测试预算" })}\n\n当前模拟预算 12 个虚构单位。^[old.md:1]\n`;

export function job(): FlowJob {
  const text = "样例素材测试预算由12个虚构单位调整为28个虚构单位，其他条件不变。";
  return { id: "session-batch-2", projectId: "companion", projectLabel: "Companion", sessionId: "s", turnId: "t2",
    cwd: "/tmp/project", createdAt: "2025-01-15T00:00:00Z", prompt: text, lastAssistant: "",
    allowedPageIds: [pageId], evidence: [{ id: "user-2", kind: "user", text, sha256: sha256Text(text),
      observedAt: "2025-01-15T00:00:00Z", locator: "codex://s/t2" }],
    sessionContext: { version: 1, revision: 1, summary: "正在讨论样例素材测试预算。", topicPageIds: [pageId], evidence: [] } };
}

export function plan(): TopicPlan {
  return { summary: "样例素材测试预算调整为28个虚构单位。", disposition: "edit", reason: "现有预算页可更新",
    pages: [{ action: "update", targetPageId: pageId, topic: "样例素材推广", decisionObject: "样例素材测试预算",
      title: "样例素材测试预算", reason: "同一对象已存在，保留旧预算历史" }] };
}

export function draft(): TopicDraft {
  const input = job();
  return { claims: [{ text: input.prompt, evidenceId: "user-2", quote: input.prompt, title: "预算调整",
    topic: "样例素材推广", decisionObject: "样例素材测试预算", slug: "sample-material-budget", targetPageId: pageId,
    kind: "decision", status: "decided", useWhen: "执行样例素材测试预算调整时", rationale: "用户明确调整样例预算",
    replacementIntent: false, supportingQuotes: [] }], pages: [{ pageId,
    body: "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n此前预算 12 个虚构单位。^[old.md:1]",
    claimIndexes: [0] }], summary: "保留用户确认的样例预算调整与旧预算历史。" };
}

export function config(responses: Record<string, unknown>, onSystem?: (system: string, toolName: string) => void): FlowConfig {
  const stateDir = mkdtempSync(path.join(tmpdir(), "consolidation-state-"));
  mkdirSync(path.join(stateDir, "wiki/sources"), { recursive: true });
  writeFileSync(path.join(stateDir, "wiki/sources/old.md"), "用户确认样例预算为12个虚构单位，适用样例素材测试。\n");
  onTestFinished(() => rm(stateDir, { recursive: true, force: true }));
  const unsupported = async (): Promise<never> => { throw new Error("unexpected provider operation"); };
  const provider: LLMProvider = { complete: unsupported, stream: unsupported, embed: unsupported,
    toolCall: async (_system, messages, tools) => {
      onSystem?.(_system, tools[0].name);
      if (!(tools[0].name in responses)) throw new Error(`unplanned tool ${tools[0].name}`);
      const response = responses[tools[0].name];
      const request = JSON.parse(messages[0].content) as Record<string, any>;
      const value = typeof response === "function" ? response(request) : response;
      return JSON.stringify(toCorrectionShape(tools[0].name, request, value));
    } };
  return { wikiRoot: path.join(stateDir, "wiki"), stateDir, model: "test", maxProposals: 5,
    maxPendingPerProject: 10, provider, reviewer: provider, machineId: "test",
    exchange: { root: "/tmp/exchange", protocolVersion: 2, participants: ["test"] } };
}

export function runConsolidation(input: FlowJob, responses: Record<string, unknown>, pages: ReadonlyMap<string, string>,
  onSystem?: (system: string, toolName: string) => void): Promise<FlowResult> {
  return consolidateSession(input, config(responses, onSystem), pages);
}

function toCorrectionShape(toolName: string, request: Record<string, any>, value: unknown): unknown {
  if (toolName === "knowledge_topic_plan" && request.correction) return { plan: value };
  if (toolName !== "knowledge_topic_edit" || !request.correction || !value || typeof value !== "object") return value;
  const options = (request.evidence ?? []).flatMap((item: any) => item.quoteOptions ?? []);
  const quoteId = (quote: string) => options.find((item: any) => item.quote === quote)?.quoteId ?? "unknown-quote";
  const draft = value as Record<string, any>;
  return { ...draft, claims: (draft.claims ?? []).map((claim: Record<string, any>) => {
    const { quote, evidenceId: _evidenceId, topic: _topic, decisionObject: _decisionObject, supportingQuotes, ...fields } = claim;
    return { ...fields, quoteId: claim.quoteId ?? quoteId(quote),
      supportingQuotes: (supportingQuotes ?? []).map((item: Record<string, any>) => ({
        quoteId: item.quoteId ?? quoteId(item.quote),
      })) };
  }) };
}

export function accepted() {
  return { decision: "accept", reason: "保留旧预算历史，原文明确支持新预算与同一对象", checkedClaimIndexes: [0], checkedPageIds: [pageId] };
}
