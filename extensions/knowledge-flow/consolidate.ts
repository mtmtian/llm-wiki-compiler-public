/**
 * Session-oriented consolidation: plan project topics, edit complete pages,
 * independently review the whole diff, and export immutable reviewed revisions.
 * The application checkpoint supplies continuity; each reviewer remains isolated.
 */
import { CodexAgentProvider } from "../../src/providers/codex-agent.js";
import type { LLMProvider } from "../../src/utils/provider.js";
import type { ClaimDecision, ClaimReview, FlowConfig, FlowJob, FlowResult } from "./types.js";
import { claimReview, finishResult } from "./claim-decisions.js";
import { createPlanTool, createQuoteBoundEditTool, createTopicReviewTool, planTool } from "./consolidation-schema.js";
import { ConsolidationOutputError, durableModel } from "./consolidation-model.js";
import { assertTopicContextBudget, resolvePlan, topicCatalog } from "./consolidation-plan.js";
import type { TopicPlan, PlannedPage } from "./consolidation-plan.js";
import { resolveQuoteBoundDraft, unchangedRevisions, UncertainClaimsError, validatedDraft, withSessionEvidence } from "./consolidation-draft.js";
import type { QuoteBoundTopicDraft, StableClaimEntry, TopicDraft } from "./consolidation-draft.js";
import { planningCorrectionPrompt, planningPrompt, planSystem, editSystem, correctionEditSystem, reviewSystem, withTopicScope } from "./consolidation-prompts.js";
import { buildCorrectionEvidence } from "./consolidation-quotes.js";
import { applyCorrectionPatch, correctionPermissionsForReview, createCorrectionPatchTool, stableDraftView } from "./claim-patch.js";
import type { CorrectionPatch, CorrectionPermissions } from "./claim-patch.js";
import { quoteContribution } from "./contribution.js";
import { priorSourceContext } from "./consolidation-sources.js";
import { validateRetirementReferences } from "./citation-retirement.js";
import { citationChecklist, unaccountedCitations, withRepairedCitations } from "./citation-repair.js";
import { withoutRejectedClaims } from "./claim-pruning.js";
import { editablePages, pageBodyBudgets, pagePublishedAt, withKeptParagraphs } from "./kept-paragraphs.js";

// Durable stage name of the review that checks a draft restricted to its accepted claims.
const PRUNED_STAGE = "pruned";
// Edit stages in order: the initial draft, then at most one validator-driven and one reviewer-driven correction.
// A reviewer-driven correction may follow a validator-driven one, because the reviewer only sees a valid draft.
const EDIT_STAGES = [undefined, "correction", "recorrection"] as const;

interface TopicReview {
  decision: "accept" | "reject" | "needs_review";
  reason: string;
  checkedClaimIndexes: number[];
  checkedPageIds: string[];
  checkedRetiredCitations?: string[];
  /** Optional to the program: nothing depends on it until the ledger gate (claim-decisions.ts). */
  claimDecisions?: ClaimDecision[];
  quoteRepairs?: Array<{ claimIndex: number; quoteId: string }>;
  replaceEvidenceForClaims?: number[];
}

interface DraftCorrection { reason: string; previousDraft: TopicDraft; stableClaims: StableClaimEntry[];
  permissions: CorrectionPermissions; review?: TopicReview; }

/** Process a bounded increment using durable session context and its complete scoped topic catalog. */
export async function consolidateSession(input: FlowJob, config: FlowConfig, existing: ReadonlyMap<string, string>): Promise<FlowResult> {
  const job = withSessionEvidence(input);
  const provider = config.provider ?? new CodexAgentProvider(config.model, { timeoutMs: 180_000 });
  const request = { stateDir: config.stateDir, jobId: job.id, model: config.model, provider };
  let plan = await durableModel<TopicPlan>({ ...request, tool: planTool, system: withTopicScope(job, planSystem),
    prompt: planningPrompt(job, existing), tokens: 5000 });
  let pages: PlannedPage[];
  let priorSources: Record<string, string>;
  try {
    pages = checkedPlan(plan, job, existing);
  } catch (error) {
    const reason = errorMessage(error);
    try {
      const corrected = await durableModel<{ plan: TopicPlan }>({ ...request, stage: "correction", tool: createPlanTool(job.allowedPageIds), system: withTopicScope(job, planSystem),
        prompt: planningCorrectionPrompt(job, existing, plan, reason), tokens: 5000 });
      plan = corrected.plan;
      pages = checkedPlan(plan, job, existing);
    } catch (correctionError) {
      return failed(job, `${reason}; ${errorMessage(correctionError)}`, plan.summary,
        !(correctionError instanceof ConsolidationOutputError));
    }
  }
  if (job.topicScope === "semantic") {
    try { assertTopicContextBudget(existing, pages.map(page => page.pageId)); }
    catch (error) { return failed(job, errorMessage(error), plan.summary); }
  }
  try {
    priorSources = await priorSourceContext(config.wikiRoot, pages);
  } catch (error) {
    return failed(job, errorMessage(error), plan.summary, true);
  }
  if (plan.disposition !== "edit") return disposition(plan, job);
  return editAndReview(job, config, { existing, plan, pages, priorSources });
}

/** Plan validation is deterministic; model transport failures keep their distinct retry contract. */
function checkedPlan(plan: TopicPlan, job: FlowJob, existing: ReadonlyMap<string, string>): PlannedPage[] {
  try { return resolvePlan(plan, job, existing); }
  catch (error) { throw new ConsolidationOutputError(errorMessage(error)); }
}

interface EditContext { existing: ReadonlyMap<string, string>; plan: TopicPlan; pages: PlannedPage[]; priorSources: Record<string, string>; }

async function editAndReview(job: FlowJob, config: FlowConfig, context: EditContext): Promise<FlowResult> {
  const provider = config.provider ?? new CodexAgentProvider(config.model, { timeoutMs: 180_000 });
  const reviewer = config.reviewer ?? new CodexAgentProvider(config.model, { timeoutMs: 180_000 });
  const run: EditRunContext = { job, config, topic: context,
    request: { stateDir: config.stateDir, jobId: job.id, model: config.model, provider }, reviewer,
    correctionCatalog: buildCorrectionEvidence(job.evidence), claimReviews: [], stableClaims: [] };
  let correction: DraftCorrection | undefined;
  for (const stage of EDIT_STAGES) {
    let outcome: EditStageResult;
    try { outcome = await runEditStage(run, stage, correction); }
    catch (error) { outcome = { result: failed(job, errorMessage(error), context.plan.summary,
      !(error instanceof ConsolidationOutputError)) }; }
    if (outcome.result) {
      return finishResult(outcome.result, run.claimReviews, { enabled: config.knowledgeLedger === true, reviewed: run.reviewed });
    }
    correction = outcome.correction;
  }
  throw new Error("consolidation attempts exhausted");
}

interface EditRunContext {
  job: FlowJob;
  config: FlowConfig;
  topic: EditContext;
  request: { stateDir: string; jobId: string; model: string; provider: LLMProvider };
  reviewer: LLMProvider;
  correctionCatalog: ReturnType<typeof buildCorrectionEvidence>;
  /** Per-claim conclusions of each review attempt, in order (claim-decisions.ts). */
  claimReviews: ClaimReview[];
  /** Run-local stable identities for the current draft, excluded from FlowClaim and publication data. */
  stableClaims: StableClaimEntry[];
  /** The contribution the latest review judged; its accepted claims may become a ledger record. */
  reviewed?: NonNullable<FlowResult["contribution"]>;
}
type DraftAttemptResult = { ok: true; draft: TopicDraft; stableClaims: StableClaimEntry[] } | { ok: false; error: string };
interface EditStageResult { result?: FlowResult; correction?: DraftCorrection; }

async function runEditStage(run: EditRunContext, stage: string | undefined,
  correction: DraftCorrection | undefined): Promise<EditStageResult> {
  const attempt = await draftAttempt(run, stage, correction);
  if (!attempt.ok) return { result: failed(run.job, attempt.error, correction?.previousDraft.summary ?? "") };
  run.stableClaims = attempt.stableClaims;
  const draft = withRepairedCitations(attempt.draft, run.topic.pages);
  return validateAndReviewStage(run, stage, correction, draft, attempt.stableClaims);
}

async function validateAndReviewStage(run: EditRunContext, stage: string | undefined,
  correction: DraftCorrection | undefined, draft: TopicDraft, stableClaims: StableClaimEntry[]): Promise<EditStageResult> {
  const outcome = await reviewedDraft(run, stage, draft);
  if ("error" in outcome) return outcome.requiresDecision
    ? { result: held(run.job, outcome.error, draft.summary) } : validationFailure(run, stage, correction, draft, stableClaims, outcome.error);
  const { review, contribution } = outcome;
  if (review.decision === "accept") return { result: acceptedReview(run.job, draft.summary, review, contribution, run.topic.pages) };
  if (finalRejection(review, correction)) return { result: await acceptedClaimsOnly(run, draft, review) };
  const reviewedClaims = stableClaims;
  try {
    const permissions = correctionPermissionsForReview(reviewedClaims, review);
    return { correction: { reason: review.reason, previousDraft: draft, stableClaims: reviewedClaims, permissions, review } };
  } catch (error) {
    return { result: failed(run.job, errorMessage(error), draft.summary) };
  }
}

type ReviewedDraft = { review: TopicReview; contribution: NonNullable<FlowResult["contribution"]> } | { error: string; requiresDecision?: boolean };

/** Validate a draft and, when it is valid, review it and record the per-claim conclusions. */
async function reviewedDraft(run: EditRunContext, stage: string | undefined, draft: TopicDraft): Promise<ReviewedDraft> {
  let contribution: NonNullable<FlowResult["contribution"]>;
  try { contribution = checkedContribution(draft, run.job, run.topic.pages, run.config.maxProposals, run.topic.priorSources); }
  catch (error) { return { error: errorMessage(error), requiresDecision: error instanceof UncertainClaimsError }; }
  const review = await reviewAttempt(run, stage, draft, contribution);
  run.claimReviews.push(claimReview(stage, review, contribution.claims.length));
  run.reviewed = contribution;
  return { review, contribution };
}

/**
 * After a final rejection, publish the accepted claims alone when they pass validation and a fresh review
 * (claim-pruning.ts). An unresolved intent remains held; an invalid edit remains a technical failure.
 */
async function acceptedClaimsOnly(run: EditRunContext, draft: TopicDraft, review: TopicReview): Promise<FlowResult> {
  const heldWith = (note?: string) => (review.decision === "needs_review" ? held : failed)(run.job,
    note ? `${review.reason}；只保留已接受的 claim 后${note}` : review.reason, draft.summary);
  const pruned = withoutRejectedClaims(draft, run.claimReviews.at(-1));
  if (!pruned) return heldWith();
  // Pages whose claims were all rejected keep their current text, so validation, the fresh review and its
  // coverage apply to the remaining pages. This is the run's last step; the same run keeps the review record.
  run.topic = narrowedTopic(run.topic, pruned);
  let outcome: ReviewedDraft;
  try { outcome = await reviewedDraft(run, PRUNED_STAGE, pruned); }
  catch (error) {
    if (review.decision === "needs_review") return heldWith(`审核失败：${errorMessage(error)}`);
    return failed(run.job, `${review.reason}；只保留已接受的 claim 后审核失败：${errorMessage(error)}`,
      draft.summary, !(error instanceof ConsolidationOutputError));
  }
  if ("error" in outcome) return heldWith(`未通过校验：${outcome.error}`);
  if (outcome.review.decision === "needs_review") return held(run.job,
    `${review.reason}；只保留已接受的 claim 后仍需确认：${outcome.review.reason}`, draft.summary);
  if (outcome.review.decision !== "accept") return heldWith(`仍未通过审核：${outcome.review.reason}`);
  return acceptedReview(run.job, pruned.summary, outcome.review, outcome.contribution, run.topic.pages);
}

/** The topic restricted to the pages a draft revises; plan pages and planned pages correspond by position (resolvePlan). */
function narrowedTopic(topic: EditContext, draft: TopicDraft): EditContext {
  const revised = new Set(draft.pages.map(page => page.pageId));
  const kept = topic.pages.flatMap((page, index) => revised.has(page.pageId) ? [index] : []);
  return { ...topic, pages: kept.map(index => topic.pages[index]),
    plan: { ...topic.plan, pages: kept.map(index => topic.plan.pages[index]) } };
}

function validationFailure(run: EditRunContext, stage: string | undefined, correction: DraftCorrection | undefined,
  draft: TopicDraft, stableClaims: StableClaimEntry[], reason: string): EditStageResult {
  if (!stage) return { correction: { reason, previousDraft: draft, stableClaims,
    permissions: { lockedClaimIds: [], replaceEvidenceForClaimIds: [] } } };
  return { result: failed(run.job, reason, draft.summary ?? correction?.previousDraft.summary ?? "") };
}

async function draftAttempt(run: EditRunContext, stage: string | undefined,
  correction: DraftCorrection | undefined): Promise<DraftAttemptResult> {
  const tool = draftTool(run, stage, correction);
  try {
    const modelDraft = await loadDraftModel(run, stage, correction, tool);
    return { ok: true, ...restoredDraft(stage, modelDraft, correction, run) };
  } catch (error) {
    if (!stage || !(error instanceof ConsolidationOutputError)) throw error;
    return { ok: false, error: `correction evidence selection failed: ${errorMessage(error)}` };
  }
}

/** Materialization errors describe invalid returned data, not an unavailable model transport. */
function restoredDraft(stage: string | undefined, modelDraft: CorrectionPatch | QuoteBoundTopicDraft,
  correction: DraftCorrection | undefined, run: EditRunContext) {
  try {
    const restored = restoreDraft(stage, modelDraft, correction, run.correctionCatalog, run.topic.pages);
    return { draft: withKeptParagraphs(restored.draft, run.topic.pages), stableClaims: restored.stableClaims };
  } catch (error) { throw new ConsolidationOutputError(errorMessage(error)); }
}

function draftTool(run: EditRunContext, stage: string | undefined, correction?: DraftCorrection) {
  return stage ? createCorrectionPatchTool(run.topic.pages.map(page => page.pageId), run.correctionCatalog,
    correction?.stableClaims ?? [], correction?.permissions ?? { lockedClaimIds: [], replaceEvidenceForClaimIds: [] })
    : createQuoteBoundEditTool(run.topic.pages.map(page => page.pageId), run.correctionCatalog);
}

async function loadDraftModel(run: EditRunContext, stage: string | undefined,
  correction: DraftCorrection | undefined,
  tool: ReturnType<typeof createCorrectionPatchTool> | ReturnType<typeof createQuoteBoundEditTool>): Promise<CorrectionPatch | QuoteBoundTopicDraft> {
  const system = withTopicScope(run.job, stage ? correctionEditSystem : editSystem);
  return durableModel<CorrectionPatch | QuoteBoundTopicDraft>({ ...run.request, stage, tool, system,
    prompt: draftPrompt(run, stage, correction), tokens: 12000 });
}

function draftPrompt(run: EditRunContext, stage: string | undefined,
  correction: DraftCorrection | undefined): string {
  const correctionContext = correction ? { diagnostics: correction.reason, review: correction.review,
    permissions: correction.permissions, previousDraft: stableDraftView(correction.previousDraft, correction.stableClaims),
    unaccountedCitations: unaccountedCitations(correction.previousDraft, run.topic.pages) } : undefined;
  return JSON.stringify({ projectId: run.job.projectId, sourceProjectId: run.job.projectId,
    ...(run.job.topicScope ? { topicScope: run.job.topicScope } : {}), currentTaskContext: run.job.prompt,
    sessionContext: run.job.sessionContext?.summary, plan: run.topic.plan,
    pages: editablePages(run.topic.pages), pageBodyBudgets: pageBodyBudgets(run.topic.pages),
    priorSources: run.topic.priorSources, evidence: run.correctionCatalog,
    maxClaims: run.config.maxProposals,
    citationChecklist: citationChecklist(run.topic.pages),
    quoteSelection: "Choose quoteId values from the frozen quoteOptions; do not write source quote text.",
    correction: correctionContext });
}

function restoreDraft(stage: string | undefined, modelDraft: CorrectionPatch | QuoteBoundTopicDraft,
  correction: DraftCorrection | undefined, catalog: ReturnType<typeof buildCorrectionEvidence>, frozenPages: readonly PlannedPage[]):
  { draft: TopicDraft; stableClaims: StableClaimEntry[] } {
  if (!stage) return resolveQuoteBoundDraft(modelDraft as QuoteBoundTopicDraft, catalog, frozenPages);
  if (!correction) throw new Error("correction context is missing");
  return applyCorrectionPatch(modelDraft as CorrectionPatch, correction.stableClaims, correction.permissions, catalog, frozenPages);
}

async function reviewAttempt(run: EditRunContext, stage: string | undefined, draft: TopicDraft,
  contribution: NonNullable<FlowResult["contribution"]>): Promise<TopicReview> {
  const retirementCitations = contribution.topicRevisions?.flatMap(page => page.citationRetirements?.map(item => item.citation) ?? []) ?? [];
  const quoteIds = stage ? [] : run.correctionCatalog.flatMap(item => item.quoteOptions.map(option => option.quoteId));
  const tool = createTopicReviewTool(contribution.claims.length, run.topic.pages.map(page => page.pageId), retirementCitations, quoteIds);
  return durableModel<TopicReview>({ ...run.request, stage, provider: run.reviewer, tool, system: withTopicScope(run.job, reviewSystem),
    prompt: JSON.stringify({ projectId: run.job.projectId, sourceProjectId: run.job.projectId,
      ...(run.job.topicScope ? { topicScope: run.job.topicScope } : {}), currentTaskContext: run.job.prompt,
      evidence: run.correctionCatalog, catalog: topicCatalog(run.topic.existing),
      existing: reviewExisting(run), priorSources: run.topic.priorSources, plan: run.topic.plan, pages: reviewPages(run),
      claims: draft.claims, revisions: contribution.topicRevisions }), tokens: 5000 });
}

function reviewExisting(run: EditRunContext): Array<[string, string]> {
  if (run.job.topicScope !== "semantic") return [...run.topic.existing];
  return run.topic.pages.flatMap(page => {
    const original = run.topic.existing.get(page.pageId);
    return original === undefined ? [] : [[page.pageId, original]];
  });
}

function reviewPages(run: EditRunContext): Array<Omit<PlannedPage, "original"> & { pagePublishedAt: string | null }> |
  Array<PlannedPage & { pagePublishedAt: string | null }> {
  const dated = run.topic.pages.map(page => ({ ...page, pagePublishedAt: pagePublishedAt(page.original) }));
  if (run.job.topicScope !== "semantic") return dated;
  return dated.map(({ original: _original, ...page }) => page);
}

/** The reviewer gets one bounded correction of its own; a draft already corrected for the reviewer is final. */
function finalRejection(review: TopicReview, correction: DraftCorrection | undefined): boolean {
  return review.decision === "needs_review" || correction?.review !== undefined;
}

function acceptedReview(job: FlowJob, summary: string, review: TopicReview,
  contribution: NonNullable<FlowResult["contribution"]>, pages: PlannedPage[]): FlowResult {
  try { verifyReviewCoverage(review, contribution.claims.length, pages); verifyRetirementReview(review, contribution); }
  catch (error) { return failed(job, errorMessage(error), summary); }
  if (!contribution.claims.length) return { status: "empty", publishedPageIds: [], reviewCount: 0,
    sessionMemory: memory(job, summary, pages.map(page => page.pageId)) };
  return { status: "submitted", publishedPageIds: [], reviewCount: 0, contribution,
    sessionMemory: memory(job, summary, pages.map(page => page.pageId)) };
}

function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "invalid consolidation output"; }

function checkedContribution(draft: TopicDraft, job: FlowJob, pages: PlannedPage[], maximum: number,
  priorSources: Record<string, string>): NonNullable<FlowResult["contribution"]> {
  const unchanged = unchangedRevisions(draft, pages);
  if (unchanged) return { claims: [], evidence: [], topicRevisions: unchanged };
  const { claims, revisions } = validatedDraft(draft, job, pages, maximum, priorSources);
  const contribution = quoteContribution(job, claims, revisions);
  const retirements = contribution.topicRevisions?.flatMap(page => page.citationRetirements ?? []) ?? [];
  validateRetirementReferences(retirements, [...contribution.evidence.map(item => item.text),
    ...Object.values(priorSources), ...pages.map(page => page.original ?? "")]);
  if (Buffer.byteLength(JSON.stringify(contribution), "utf8") > 100_000) throw new Error("topic contribution exceeds publication byte budget");
  return contribution;
}

function held(job: FlowJob, reason: string, summary: string): FlowResult {
  return { status: "needs_review", publishedPageIds: [], reviewCount: 1, error: reason,
    sessionMemory: memory(job, `未发布（待审）：${reason}\n${summary}`, []) };
}

/** Preserve diagnostics and inputs while separating machine failure from a request for user intent. */
function failed(job: FlowJob, reason: string, summary: string, retryable = false): FlowResult {
  return { status: "error", publishedPageIds: [], reviewCount: 0, error: reason,
    ...(retryable === false ? { retryable } : {}), sessionMemory: memory(job, `未发布（技术失败）：${reason}\n${summary}`, []) };
}

function verifyReviewCoverage(review: TopicReview, count: number, pages: PlannedPage[]): void {
  const indexes = new Set(review.checkedClaimIndexes); const ids = new Set(review.checkedPageIds);
  if (indexes.size !== count || review.checkedClaimIndexes.length !== count
    || Array.from({ length: count }, (_, index) => index).some(index => !indexes.has(index))) {
    throw new Error(`independent review claim coverage mismatch: expected indexes 0-${Math.max(0, count - 1)}; omitted or duplicate claim indexes`);
  }
  if (ids.size !== pages.length || review.checkedPageIds.length !== pages.length
    || pages.some(page => !ids.has(page.pageId))) {
    throw new Error(`independent review page coverage mismatch: only revised page IDs are allowed; omitted, duplicate or extra page IDs`);
  }
}

function verifyRetirementReview(review: TopicReview, contribution: NonNullable<FlowResult["contribution"]>): void {
  const expected = new Set(contribution.topicRevisions?.flatMap(page => page.citationRetirements?.map(item => item.citation) ?? []));
  const checked = review.checkedRetiredCitations ?? [];
  if (checked.length !== expected.size || new Set(checked).size !== expected.size || checked.some(item => !expected.has(item))) {
    throw new Error("independent review omitted an evidence retirement");
  }
}

function disposition(plan: TopicPlan, job: FlowJob): FlowResult {
  return { status: plan.disposition === "noop" ? "empty" : "needs_review", publishedPageIds: [],
    reviewCount: plan.disposition === "needs_review" ? 1 : 0, error: plan.reason, sessionMemory: memory(job, plan.summary, []) };
}

function memory(job: FlowJob, summary: string, pageIds: string[]): NonNullable<FlowResult["sessionMemory"]> {
  const retained = (job.sessionContext?.topicPageIds ?? []).filter(id => job.allowedPageIds.includes(id));
  return { summary, topicPageIds: [...new Set([...retained, ...pageIds])] };
}
