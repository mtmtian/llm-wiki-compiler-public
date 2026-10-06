/**
 * Session-oriented consolidation: plan project topics, edit complete pages,
 * independently review the whole diff, and export immutable reviewed revisions.
 * The application checkpoint supplies continuity; each reviewer remains isolated.
 */
import { CodexAgentProvider } from "../../src/providers/codex-agent.js";
import type { LLMProvider } from "../../src/utils/provider.js";
import type { ClaimReview, FlowConfig, FlowJob, FlowResult } from "./types.js";
import { claimReview, finishResult } from "./claim-decisions.js";
import { createQuoteBoundEditTool, createTopicReviewTool } from "./consolidation-schema.js";
import { ConsolidationOutputError, durableModel } from "./consolidation-model.js";
import { TopicBodyLimitError, topicCatalog } from "./consolidation-plan.js";
import type { PlannedPage } from "./consolidation-plan.js";
import { resolveQuoteBoundDraft, unchangedRevisions, UncertainClaimsError, validatedDraft, withSessionEvidence } from "./consolidation-draft.js";
import type { QuoteBoundTopicDraft, StableClaimEntry, TopicDraft } from "./consolidation-draft.js";
import { editSystem, correctionEditSystem, reviewSystem, withTopicScope } from "./consolidation-prompts.js";
import { buildCorrectionEvidence } from "./consolidation-quotes.js";
import { applyCorrectionPatch, correctionPermissionsForReview, createCorrectionPatchTool, stableDraftView } from "./claim-patch.js";
import type { CorrectionPatch, CorrectionPermissions } from "./claim-patch.js";
import { quoteContribution } from "./contribution.js";
import { prepareConsolidation } from "./consolidation-planning.js";
import type { EditContext } from "./consolidation-planning.js";
import { errorMessage, failed, held, memory } from "./consolidation-outcomes.js";
import { reviewContext } from "./consolidation-review-context.js";
import type { PreviousReview, TopicReview } from "./consolidation-review-context.js";
import { validateRetirementReferences } from "./citation-retirement.js";
import { citationChecklist, unaccountedCitations, withRepairedCitations } from "./citation-repair.js";
import { withoutRejectedClaims } from "./claim-pruning.js";
import { editablePages, pageBodyBudgets, pagePublishedAt, withKeptParagraphs } from "./kept-paragraphs.js";

// Durable stage name of the review that checks a draft restricted to its accepted claims.
const PRUNED_STAGE = "pruned";
// Edit stages in order: the initial draft, then at most one validator-driven and one reviewer-driven correction.
// A reviewer-driven correction may follow a validator-driven one, because the reviewer only sees a valid draft.
const EDIT_STAGES = [undefined, "correction", "recorrection"] as const;

interface DraftCorrection { reason: string; previousDraft: TopicDraft; stableClaims: StableClaimEntry[];
  permissions: CorrectionPermissions; review?: TopicReview; }

/** Process a bounded increment using durable session context and its complete scoped topic catalog. */
export async function consolidateSession(input: FlowJob, config: FlowConfig, existing: ReadonlyMap<string, string>): Promise<FlowResult> {
  const job = withSessionEvidence(input);
  const prepared = await prepareConsolidation(job, config, existing);
  if ("result" in prepared) return prepared.result;
  try {
    return await editAndReview(job, config, prepared.topic);
  } catch (error) {
    if (!(error instanceof TopicBodyLimitError)) throw error;
    try {
      const recovered = await prepareConsolidation(job, config, existing, { previousPlan: prepared.topic.plan, error });
      return "result" in recovered ? recovered.result : await editAndReview(job, config, recovered.topic);
    } catch (recoveryError) {
      return failed(job, errorMessage(recoveryError), prepared.topic.plan.summary,
        !(recoveryError instanceof ConsolidationOutputError || recoveryError instanceof TopicBodyLimitError));
    }
  }
}

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
    catch (error) {
      if (error instanceof TopicBodyLimitError) throw error;
      outcome = { result: failed(job, errorMessage(error), context.plan.summary,
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
  const previous: PreviousReview | undefined = correction ? { mode: "correction", draft: correction.previousDraft,
    stableClaims: correction.stableClaims, review: correction.review } : undefined;
  const outcome = await reviewedDraft(run, stage, draft, previous);
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
async function reviewedDraft(run: EditRunContext, stage: string | undefined, draft: TopicDraft, previous?: PreviousReview): Promise<ReviewedDraft> {
  let contribution: NonNullable<FlowResult["contribution"]>;
  try { contribution = checkedContribution(draft, run.job, run.topic.pages, run.config.maxProposals, run.topic.priorSources); }
  catch (error) {
    // Preserve the initial same-page compression attempt before spending the single replan.
    if (error instanceof TopicBodyLimitError && (stage || run.topic.attempt)) throw error;
    return { error: errorMessage(error), requiresDecision: error instanceof UncertainClaimsError };
  }
  const review = await reviewAttempt(run, stage, draft, contribution, previous);
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
  const previous: PreviousReview = { mode: "accepted_subset", draft, stableClaims: run.stableClaims, review };
  run.stableClaims = run.stableClaims.filter(entry => pruned.claims.includes(entry.claim));
  // Pages whose claims were all rejected keep their current text, so validation, the fresh review and its
  // coverage apply to the remaining pages. This is the run's last step; the same run keeps the review record.
  run.topic = narrowedTopic(run.topic, pruned);
  let outcome: ReviewedDraft;
  try { outcome = await reviewedDraft(run, PRUNED_STAGE, pruned, previous); }
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
  return durableModel<CorrectionPatch | QuoteBoundTopicDraft>({ ...run.request, stage: modelStage(run, stage), tool, system,
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
  contribution: NonNullable<FlowResult["contribution"]>, previous?: PreviousReview): Promise<TopicReview> {
  const retirementCitations = contribution.topicRevisions?.flatMap(page => page.citationRetirements?.map(item => item.citation) ?? []) ?? [];
  const quoteIds = stage ? [] : run.correctionCatalog.flatMap(item => item.quoteOptions.map(option => option.quoteId));
  const tool = createTopicReviewTool(contribution.claims.length, run.topic.pages.map(page => page.pageId), retirementCitations, quoteIds);
  return durableModel<TopicReview>({ ...run.request, stage: modelStage(run, stage), provider: run.reviewer, tool, system: withTopicScope(run.job, reviewSystem),
    prompt: JSON.stringify({ projectId: run.job.projectId, sourceProjectId: run.job.projectId,
      ...(run.job.topicScope ? { topicScope: run.job.topicScope } : {}), currentTaskContext: run.job.prompt,
      evidence: run.correctionCatalog, catalog: topicCatalog(run.topic.existing),
      existing: reviewExisting(run), priorSources: run.topic.priorSources, plan: run.topic.plan, pages: reviewPages(run),
      claims: draft.claims, revisions: contribution.topicRevisions,
      reviewContext: reviewContext(draft, run.stableClaims, previous) }), tokens: 5000 });
}

/** Replanning uses independent durable slots; internal correction semantics stay unchanged. */
function modelStage(run: EditRunContext, stage?: string): string | undefined {
  return run.topic.attempt ? [run.topic.attempt, stage].filter(Boolean).join("-") : stage;
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
