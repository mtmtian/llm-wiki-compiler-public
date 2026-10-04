/**
 * Orchestrates legacy project scope and activated semantic topic scope.
 *
 * A job is rejected outside its explicit page scope, extracted into at most
 * five evidence-bound claims, independently reviewed, and only then sent to
 * the compiler candidate/approval path. Review conflicts live under stateDir;
 * they never become wiki sources or context by accident.
 */

import { existsSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { sha256Text } from "../../src/connectors/hash.js";
import { CodexAgentProvider } from "../../src/providers/codex-agent.js";
import type { LLMProvider } from "../../src/utils/provider.js";
import { isSafeFilenameComponent } from "../../src/profile/identity.js";
import { atomicWrite, parseFrontmatter } from "../../src/utils/markdown.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { extractClaims, validateClaims } from "./extract.js";
import { loadPublishAttempt, publishClaims } from "./publish.js";
import type { PublishResult } from "./publish.js";
import { reviewClaims } from "./review.js";
import { claimIdentity, publishedClaimIds } from "./claim-identity.js";
import { MAX_PROPOSALS } from "./types.js";
import { assertTopicContextBudget, MAX_TOPIC_CONTEXT_CHARS } from "./consolidation-plan.js";
import { quoteContribution } from "./contribution.js";
import { consolidateSession } from "./consolidate.js";
import { ConsolidationOutputError } from "./consolidation-model.js";
import { pendingReviewCount } from "./review-capacity.js";
import type { FlowClaim, FlowConfig, FlowDependencies, FlowJob, FlowResult, FlowReviewDecision } from "./types.js";

const DEFAULT_DEPENDENCIES: FlowDependencies = { extract: extractClaims, review: reviewClaims };

interface ReviewEvidence { id: string; kind: "user" | "assistant" | "artifact"; quote: string; locator: string; observedAt: string; }

/** Process one completed job through extraction, independent review and publish. */
export async function processJob(
  job: FlowJob,
  config: FlowConfig,
  dependencies: FlowDependencies = DEFAULT_DEPENDENCIES,
): Promise<FlowResult> {
  const invalid = validateJob(job, config);
  if (invalid) return failure(invalid);
  if (job.topicScope === "semantic" && dependencies !== DEFAULT_DEPENDENCIES) return failure("semantic jobs require the session pipeline");
  const previous = await readAudit(config.stateDir, job.id);
  if (previous) return previous;
  const attempt = await loadPublishAttempt(config.stateDir, job.id);
  if (attempt && !canPublish(config)) return failure("legacy publication recovery requires its original v1 publisher and Wiki");
  const resumed = await resumeAttempt(attempt, job, config);
  if (resumed) return resumed;
  let existing: ReadonlyMap<string, string>;
  try {
    existing = await readAllowedPages(job, config.wikiRoot);
  } catch (error) {
    return failure(error instanceof Error ? error.message : "could not read scoped wiki pages");
  }
  const queue = await pendingReviewCount(config.stateDir, job.projectId, job.reviewRetryOf);
  if (queue >= config.maxPendingPerProject) return { status: "deferred", publishedPageIds: [], reviewCount: 0, error: "review queue is full" };
  if (job.evidence.length === 0) return empty(config, job, "no evidence supplied");
  if (dependencies === DEFAULT_DEPENDENCIES && !job.submittedClaims && job.sessionContext
    && config.exchange?.protocolVersion === 2 && config.sessionConsolidation?.enabled !== false) {
    return runSession(job, config, existing);
  }
  return runModels(job, config, dependencies, existing);
}

/** Old frozen jobs keep their original contract; new session batches use whole-topic review. */
async function runSession(job: FlowJob, config: FlowConfig, existing: ReadonlyMap<string, string>): Promise<FlowResult> {
  try {
    const result = await consolidateSession(job, config, existing);
    if (result.status === "needs_review") {
      result.reviewFile = await writeReview(config.stateDir, job, [], [{ decision: "needs_review", reason: result.error }]);
    }
    return result.status === "error" && result.retryable !== false ? result : await record(job, config, result);
  } catch (error) {
    const result = failure(error instanceof Error ? error.message : "session consolidation failed",
      error instanceof ConsolidationOutputError ? false : undefined);
    return result.retryable === false ? record(job, config, result) : result;
  }
}

async function resumeAttempt(attempt: Awaited<ReturnType<typeof loadPublishAttempt>>, job: FlowJob, config: FlowConfig): Promise<FlowResult | null> {
  if (!attempt) return null;
  if (attempt.status === "completed" && attempt.result) return finishPublication(job, config, attempt.result);
  if (attempt.status !== "publishing") return null;
  try {
    const result = await publishClaims(config.wikiRoot, config.stateDir, job, attempt.claims, new Map(attempt.existing));
    return await finishPublication(job, config, result);
  } catch (error) {
    return failure(error instanceof Error ? error.message : "knowledge publish resume failed");
  }
}

async function runModels(job: FlowJob, config: FlowConfig, dependencies: FlowDependencies, existing: ReadonlyMap<string, string>): Promise<FlowResult> {
  try {
    const extractor = config.provider ?? new CodexAgentProvider(config.model, { timeoutMs: 90_000 });
    const proposals = await proposalsForJob(extractor, job, config, dependencies, existing);
    if (proposals.length > boundedMax(config.maxProposals)) return failure("extraction exceeded proposal limit");
    const published = publishedClaimIds(existing);
    const claims = config.exchange?.protocolVersion === 2 ? proposals
      : proposals.filter(claim => !published.has(claimIdentity(job.projectId, claim)));
    if (claims.length === 0) return empty(config, job, "no durable claim");
    const reviewer = config.reviewer ?? new CodexAgentProvider(config.model, { timeoutMs: 90_000 });
    const decisions = await dependencies.review(reviewer, job, claims, existing);
    return await handleDecisions(job, config, claims, decisions, existing);
  } catch (error) {
    return failure(error instanceof Error ? error.message : "knowledge flow failed");
  }
}

/** Transported proposals retain their exact evidence and must pass current scope. */
async function proposalsForJob(provider: LLMProvider, job: FlowJob, config: FlowConfig,
  dependencies: FlowDependencies, existing: ReadonlyMap<string, string>): Promise<FlowClaim[]> {
  if (!job.submittedClaims) return dependencies.extract(provider, job, existing, boundedMax(config.maxProposals));
  const claims = validateClaims(job.submittedClaims, job.evidence, job.allowedPageIds, boundedMax(config.maxProposals));
  if (claims.length !== job.submittedClaims.length) throw new Error("shared claims require valid current scope and exact evidence");
  return claims;
}

async function handleDecisions(
  job: FlowJob,
  config: FlowConfig,
  claims: FlowClaim[],
  decisions: Awaited<ReturnType<FlowDependencies["review"]>>,
  existing: ReadonlyMap<string, string>,
): Promise<FlowResult> {
  const byIndex = new Map(decisions.map((item) => [item.index, item]));
  const accepted = claims.filter((claim, index) => claim.status !== "uncertain" && !claim.replacementIntent && !hasConflict(byIndex.get(index)) && byIndex.get(index)?.decision === "accept");
  const uncertain = claims.filter((claim, index) => needsHumanReview(claim, byIndex.get(index)));
  const reviewFile = uncertain.length > 0 ? await writeReview(config.stateDir, job, uncertain, decisions) : undefined;
  if (accepted.length === 0) {
    if (reviewFile) return await record(job, config, { status: "needs_review", publishedPageIds: [], reviewCount: uncertain.length, reviewFile });
    return await record(job, config, { status: "empty", publishedPageIds: [], reviewCount: 0 });
  }
  if (config.exchange?.protocolVersion === 2 || !canPublish(config))
    return submitContribution(job, config, accepted, reviewFile, uncertain.length);
  const published = await publishClaims(config.wikiRoot, config.stateDir, job, accepted, existing);
  return finishPublication(job, config, published);
}

/** Shared mode never allows a contributor to modify compiled wiki state. */
function canPublish(config: FlowConfig): boolean {
  if (!config.exchange) return config.publishEnabled !== false;
  return config.publishEnabled === true && config.machineId === config.exchange.publisherMachineId;
}

/** Share accepted quotes only, not full transcripts or local operational state. */
async function submitContribution(job: FlowJob, config: FlowConfig, claims: FlowClaim[], reviewFile: string | undefined, reviewCount: number): Promise<FlowResult> {
  if (!config.exchange || !config.machineId) return failure("contributor requires an exchange and machine identity");
  return record(job, config, { status: "submitted", publishedPageIds: [], reviewCount,
    ...(reviewFile ? { reviewFile } : {}), contribution: quoteContribution(job, claims) });
}

/** Reconstruct pending review and its count even after publication interrupted the job. */
async function finishPublication(job: FlowJob, config: FlowConfig, published: PublishResult): Promise<FlowResult> {
  let reviewFile = await existingReviewFile(config.stateDir, job.id);
  if (published.blockedClaims.length) reviewFile = await writeReview(config.stateDir, job, published.blockedClaims,
    [{ decision: "needs_review", reason: "Publication blocked by a changed, oversized or pending page" }]);
  const review = reviewFile ? await readReview(reviewFile) : null;
  const result = publishFlowResult(published, 0, reviewFile);
  result.reviewCount = review?.claims.length ?? 0;
  return record(job, config, result);
}

function hasConflict(decision: FlowReviewDecision | undefined): boolean {
  return (decision?.conflictingPageIds.length ?? 0) > 0;
}

function needsHumanReview(claim: FlowClaim, decision: FlowReviewDecision | undefined): boolean {
  if (decision?.decision === "reject") return false;
  return claim.status === "uncertain" || claim.replacementIntent === true || !decision || hasConflict(decision) || decision.decision === "needs_review";
}

function publishFlowResult(published: PublishResult, reviewCount: number, reviewFile?: string): FlowResult {
  const result: FlowResult = { status: published.pageIds.length > 0 ? "published" : "needs_review", publishedPageIds: published.pageIds, reviewCount: reviewCount + published.blockedClaims.length, candidateIds: published.candidateIds, reviewFile };
  if (!result.reviewFile) delete result.reviewFile;
  return result;
}

function validateJob(job: FlowJob, config: FlowConfig): string | null {
  if (!validJobIdentity(job)) return "invalid project scope";
  if (!validConfigPaths(config)) return "paths must be absolute";
  if (!validFlowLimits(config)) return "invalid flow limits";
  const topicScopeError = validateTopicScope(job, config);
  if (topicScopeError) return topicScopeError;
  return validateAllowedPages(job.allowedPageIds);
}

function validateTopicScope(job: FlowJob, config: FlowConfig): string | null {
  const jobScope: unknown = job.topicScope;
  const configScope: unknown = config.topicScope;
  if (!validTopicScope(jobScope)) return "invalid job topic scope";
  if (!validTopicScope(configScope)) return "invalid configured topic scope";
  if (jobScope !== "semantic") return null;
  return semanticPrerequisiteError(job, config);
}

function validTopicScope(scope: unknown): boolean {
  return scope === undefined || scope === "semantic";
}

function semanticPrerequisiteError(job: FlowJob, config: FlowConfig): string | null {
  const prerequisites = [config.topicScope === "semantic", Boolean(job.sessionContext),
    config.exchange?.protocolVersion === 2, config.sessionConsolidation?.enabled !== false,
    job.submittedClaims === undefined];
  return prerequisites.every(Boolean) ? null
    : "semantic topic scope requires enabled configuration, a session context, exchange v2 and no submitted claims";
}

function validJobIdentity(job: FlowJob): boolean {
  return Boolean(job.id && job.projectId && job.projectLabel && path.isAbsolute(job.cwd));
}

function validConfigPaths(config: FlowConfig): boolean {
  return path.isAbsolute(config.wikiRoot) && path.isAbsolute(config.stateDir);
}

function validFlowLimits(config: FlowConfig): boolean {
  return boundedMax(config.maxProposals) >= 1 && Number.isInteger(config.maxPendingPerProject) && config.maxPendingPerProject >= 1;
}

function validateAllowedPages(ids: string[]): string | null {
  if (!Array.isArray(ids)) return "page scope is invalid";
  if (new Set(ids).size !== ids.length) return "duplicate allowed page id";
  return ids.some((id) => !validPageId(id)) ? "page scope is invalid" : null;
}

function validPageId(id: string): boolean {
  const [namespace, slug, ...extra] = id.split("/");
  return namespace === "concepts" && extra.length === 0 && typeof slug === "string" && isSafeFilenameComponent(slug);
}

function boundedMax(max: number): number {
  return Number.isInteger(max) ? Math.max(0, Math.min(MAX_PROPOSALS, max)) : 0;
}

/**
 * Largest scoped page body a job will read. Frontmatter is excluded: it is generated provenance (sources,
 * claim and publication references) that grows with every revision and merge, so counting it would let one
 * long-lived page stop every job that can see it. Prompt size stays bounded by the topic context budget.
 */
const MAX_SCOPED_PAGE_BODY_CHARS = 12_000;

async function readAllowedPages(job: FlowJob, wikiRoot: string): Promise<ReadonlyMap<string, string>> {
  const entries = await Promise.all(job.allowedPageIds.map(async (id) => {
    const safe = await confineUnderRoot(path.join("wiki", `${id}.md`), wikiRoot, { mustExist: false });
    const body = await readFile(safe, "utf8");
    if (parseFrontmatter(body).body.length > MAX_SCOPED_PAGE_BODY_CHARS) throw new Error("scoped page exceeds review budget");
    return [id, body] as const;
  }));
  const pages = new Map(entries);
  if (job.topicScope === "semantic") {
    assertTopicContextBudget(pages);
    return pages;
  }
  const total = [...pages.values()].reduce((sum, body) => sum + body.length, 0);
  if (total > MAX_TOPIC_CONTEXT_CHARS) throw new Error(`scoped wiki context exceeds ${MAX_TOPIC_CONTEXT_CHARS} characters`);
  return pages;
}

async function writeReview(stateDir: string, job: FlowJob, claims: FlowClaim[], decisions: unknown[]): Promise<string> {
  const file = path.join(stateDir, "review", `${safeId(job.id)}.json`);
  await mkdir(path.dirname(file), { recursive: true });
  const evidence: ReviewEvidence[] = claims.flatMap((claim) => {
    const item = job.evidence.find((entry) => entry.id === claim.evidenceId);
    return item ? [{ id: item.id, kind: item.kind, quote: claim.quote, locator: item.locator, observedAt: item.observedAt }] : [];
  });
  const prior = await readReview(file);
  const allClaims = [...(prior?.claims ?? []), ...claims].filter((claim, index, all) => all.findIndex((item) => item.text === claim.text) === index);
  const allEvidence = [...(prior?.evidence ?? []), ...evidence].filter((item, index, all) => all.findIndex((entry) => entry.id === item.id && entry.quote === item.quote) === index);
  const allDecisions = [...(prior?.decisions ?? []), ...decisions];
  const sessionReview = job.sessionContext ? { sessionContext: job.sessionContext, inputEvidence: job.evidence,
    modelStages: path.join(stateDir, "consolidation", sha256Text(job.id)) } : undefined;
  await atomicWrite(file, JSON.stringify({ jobId: job.id, projectId: job.projectId, createdAt: job.createdAt,
    ...(job.reviewRetryOf ? { reviewRetryOf: job.reviewRetryOf } : {}),
    claims: allClaims, evidence: allEvidence, decisions: allDecisions, sessionReview }, null, 2), { confineRoot: stateDir });
  return file;
}

async function readReview(file: string): Promise<{ claims: FlowClaim[]; evidence: ReviewEvidence[]; decisions: unknown[] } | null> {
  try {
    const value = JSON.parse(await readFile(file, "utf8")) as { claims?: FlowClaim[]; evidence?: ReviewEvidence[]; decisions?: unknown[] };
    return { claims: value.claims ?? [], evidence: value.evidence ?? [], decisions: value.decisions ?? [] };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

async function existingReviewFile(stateDir: string, jobId: string): Promise<string | undefined> {
  const file = path.join(stateDir, "review", `${safeId(jobId)}.json`);
  return existsSync(file) ? file : undefined;
}

async function readAudit(stateDir: string, jobId: string): Promise<FlowResult | null> {
  const file = path.join(stateDir, "audit", `${safeId(jobId)}.json`);
  if (!existsSync(file)) return null;
  try {
    const value = JSON.parse(await readFile(file, "utf8")) as FlowResult;
    return {
      status: value.status,
      publishedPageIds: Array.isArray(value.publishedPageIds) ? value.publishedPageIds : [],
      reviewCount: typeof value.reviewCount === "number" ? value.reviewCount : 0,
      ...(Array.isArray(value.candidateIds) ? { candidateIds: value.candidateIds } : {}),
      ...(typeof value.reviewFile === "string" ? { reviewFile: value.reviewFile } : {}),
      ...(typeof value.error === "string" ? { error: value.error } : {}),
      ...recoverableFields(value),
    };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

/** Replay the same terminal classification and accepted claims until durable host finalization succeeds. */
function recoverableFields(value: FlowResult): Partial<FlowResult> {
  return { ...(typeof value.retryable === "boolean" ? { retryable: value.retryable } : {}),
    ...(Array.isArray(value.claimReviews) ? { claimReviews: value.claimReviews } : {}),
    ...(value.ledgerContribution ? { ledgerContribution: value.ledgerContribution } : {}),
    ...(value.contribution ? { contribution: value.contribution } : {}),
    ...(value.sessionMemory ? { sessionMemory: value.sessionMemory } : {}) };
}

async function record(job: FlowJob, config: FlowConfig, result: FlowResult): Promise<FlowResult> {
  const file = path.join(config.stateDir, "audit", `${safeId(job.id)}.json`);
  await mkdir(path.dirname(file), { recursive: true });
  await atomicWrite(file, JSON.stringify({ ...result, jobId: job.id, projectId: job.projectId, recordedAt: new Date().toISOString() }, null, 2), { confineRoot: config.stateDir });
  return result;
}

async function empty(config: FlowConfig, job: FlowJob, _reason: string): Promise<FlowResult> {
  return record(job, config, { status: "empty", publishedPageIds: [], reviewCount: 0 });
}

function failure(error: string, retryable?: boolean): FlowResult {
  return { status: "error", publishedPageIds: [], reviewCount: 0, error, ...(retryable === false ? { retryable } : {}) };
}

function safeId(id: string): string {
  return id.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 120) || "job";
}
