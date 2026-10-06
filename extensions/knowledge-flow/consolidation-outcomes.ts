/** Terminal consolidation results preserve session continuity while distinguishing intent from machine failure. */
import type { FlowJob, FlowResult } from "./types.js";
import type { TopicPlan } from "./consolidation-plan.js";

/** Keep existing, in-scope destinations alongside any newly reviewed ones. */
export function memory(job: FlowJob, summary: string, pageIds: string[]): NonNullable<FlowResult["sessionMemory"]> {
  const retained = (job.sessionContext?.topicPageIds ?? []).filter(id => job.allowedPageIds.includes(id));
  return { summary, topicPageIds: [...new Set([...retained, ...pageIds])] };
}

/** Only unresolved human intent belongs in the manual review queue. */
export function held(job: FlowJob, reason: string, summary: string): FlowResult {
  return { status: "needs_review", publishedPageIds: [], reviewCount: 1, error: reason,
    sessionMemory: memory(job, `未发布（待审）：${reason}\n${summary}`, []) };
}

/** Machine failures preserve diagnostics without creating a manual review item. */
export function failed(job: FlowJob, reason: string, summary: string, retryable = false): FlowResult {
  return { status: "error", publishedPageIds: [], reviewCount: 0, error: reason,
    ...(retryable === false ? { retryable } : {}), sessionMemory: memory(job, `未发布（技术失败）：${reason}\n${summary}`, []) };
}

/** A valid non-edit plan ends before the editor, with its original reason. */
export function disposition(plan: TopicPlan, job: FlowJob): FlowResult {
  return { status: plan.disposition === "noop" ? "empty" : "needs_review", publishedPageIds: [],
    reviewCount: plan.disposition === "needs_review" ? 1 : 0, error: plan.reason, sessionMemory: memory(job, plan.summary, []) };
}

/** Do not serialize arbitrary thrown objects into a diagnostic or model prompt. */
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "invalid consolidation output"; }
