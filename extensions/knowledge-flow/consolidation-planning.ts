/** Plan and freeze one edit attempt, with a separate durable identity for bounded capacity recovery. */
import { CodexAgentProvider } from "../../src/providers/codex-agent.js";
import type { FlowConfig, FlowJob, FlowResult } from "./types.js";
import { createPlanTool, planTool } from "./consolidation-schema.js";
import { ConsolidationOutputError, durableModel } from "./consolidation-model.js";
import { assertTopicContextBudget, MAX_TOPIC_BODY_CHARS, resolvePlan } from "./consolidation-plan.js";
import type { PlannedPage, TopicPlan, TopicBodyLimitError } from "./consolidation-plan.js";
import { planningCorrectionPrompt, planningPrompt, planSystem, withTopicScope } from "./consolidation-prompts.js";
import { priorSourceContext } from "./consolidation-sources.js";
import { disposition, errorMessage, failed } from "./consolidation-outcomes.js";

export interface EditContext {
  existing: ReadonlyMap<string, string>;
  plan: TopicPlan;
  pages: PlannedPage[];
  priorSources: Record<string, string>;
  attempt?: "capacity";
}

interface CapacityRecovery { previousPlan: TopicPlan; error: TopicBodyLimitError }
type Prepared = { topic: EditContext } | { result: FlowResult };

/** Resolve a plan against frozen scope and load the exact cited source files before editing. */
export async function prepareConsolidation(job: FlowJob, config: FlowConfig, existing: ReadonlyMap<string, string>,
  recovery?: CapacityRecovery): Promise<Prepared> {
  const provider = config.provider ?? new CodexAgentProvider(config.model, { timeoutMs: 180_000 });
  const request = { stateDir: config.stateDir, jobId: job.id, model: config.model, provider };
  const system = withTopicScope(job, planSystem);
  let plan: TopicPlan;
  if (recovery) {
    const corrected = await durableModel<{ plan: TopicPlan }>({ ...request, stage: "capacity", tool: createPlanTool(job.allowedPageIds),
      system, prompt: capacityPrompt(job, existing, recovery), tokens: 5000 });
    plan = corrected.plan;
  } else plan = await durableModel<TopicPlan>({ ...request, tool: planTool, system, prompt: planningPrompt(job, existing), tokens: 5000 });
  let pages: PlannedPage[];
  try { pages = checkedPlan(plan, job, existing); }
  catch (error) {
    if (recovery) return { result: failed(job, errorMessage(error), plan.summary) };
    try {
      const corrected = await durableModel<{ plan: TopicPlan }>({ ...request, stage: "correction", tool: createPlanTool(job.allowedPageIds),
        system, prompt: planningCorrectionPrompt(job, existing, plan, errorMessage(error)), tokens: 5000 });
      plan = corrected.plan;
      pages = checkedPlan(plan, job, existing);
    } catch (correctionError) {
      return { result: failed(job, `${errorMessage(error)}; ${errorMessage(correctionError)}`, plan.summary,
        !(correctionError instanceof ConsolidationOutputError)) };
    }
  }
  return freezeContext(job, config, existing, plan, pages, recovery ? "capacity" : undefined);
}

/** Non-edit dispositions need no source reads; an unreadable selected source remains retryable. */
async function freezeContext(job: FlowJob, config: FlowConfig, existing: ReadonlyMap<string, string>,
  plan: TopicPlan, pages: PlannedPage[], attempt?: "capacity"): Promise<Prepared> {
  if (job.topicScope === "semantic") {
    try { assertTopicContextBudget(existing, pages.map(page => page.pageId)); }
    catch (error) { return { result: failed(job, errorMessage(error), plan.summary) }; }
  }
  if (plan.disposition !== "edit") return { result: disposition(plan, job) };
  try { return { topic: { existing, plan, pages, attempt, priorSources: await priorSourceContext(config.wikiRoot, pages) } }; }
  catch (error) { return { result: failed(job, errorMessage(error), plan.summary, true) }; }
}

/** Deterministic plan errors must not consume transport retries. */
function checkedPlan(plan: TopicPlan, job: FlowJob, existing: ReadonlyMap<string, string>): PlannedPage[] {
  try { return resolvePlan(plan, job, existing); }
  catch (error) { throw new ConsolidationOutputError(errorMessage(error)); }
}

/** Capacity recovery may select a real sub-workstream, but cannot reinterpret the source's intent. */
function capacityPrompt(job: FlowJob, existing: ReadonlyMap<string, string>, recovery: CapacityRecovery): string {
  return JSON.stringify({ base: JSON.parse(planningPrompt(job, existing)), correction: {
    previousPlan: recovery.previousPlan, reason: recovery.error.message,
    capacity: { pageId: recovery.error.pageId, bodyChars: recovery.error.bodyChars, maxBodyChars: MAX_TOPIC_BODY_CHARS },
    instruction: "Return the revised plan in the plan field. The restored page body exceeded capacity. Reuse another suitable page, "
      + "or create a genuinely distinct durable sub-workstream and explain its boundary with the full page. Do not duplicate or rename the same "
      + "decision object merely to evade capacity. Preserve the original evidence, scope and required knowledge. A compact edit must fit after "
      + "keep expansion and preserve necessary evidence. Capacity or unavailable full text is a technical limitation, never a reason for needs_review. "
      + "The editor will receive complete originals and cited sources for selected pages; the planner needs no filesystem tools. "
      + "Use noop only when there is no durable change, and needs_review only for genuinely unresolved user intent; either requires pages=[].",
  } });
}
