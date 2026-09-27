/**
 * Candidate rendering and promotion for accepted knowledge claims.
 *
 * Sources are written only after the independent reviewer accepts a claim, and
 * contain the supporting quote plus locator metadata. Promotion delegates to
 * the compiler's locked `review approve` path so index, links, source state and
 * embeddings follow the same rules as normal compiler approvals.
 */

import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { sha256Text } from "../../src/connectors/hash.js";
import { listCandidates, writeCandidate } from "../../src/compiler/candidates.js";
import type { ReviewCandidate } from "../../src/utils/types.js";
import { approveUnderLock } from "../../src/commands/review-approve.js";
import { acquireLock, releaseLock } from "../../src/utils/lock.js";
import { readCandidate } from "../../src/compiler/candidate-read.js";
import { atomicWrite, buildFrontmatter, parseFrontmatter } from "../../src/utils/markdown.js";
import { isSafeFilenameComponent } from "../../src/profile/identity.js";
import { replayJournal } from "../../src/trust/journal.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import type { FlowClaim, FlowEvidence, FlowJob, FlowResult } from "./types.js";
import { claimIdentity } from "./claim-identity.js";

const MAX_EXISTING_PAGE_CHARS = 12_000;

/** Claims that could not be safely published because of a concurrent/size guard. */
export interface PublishResult {
  pageIds: string[];
  candidateIds: string[];
  blockedClaims: FlowClaim[];
}

/** Durable attempt journal used to resume a publish without re-running models. */
export interface PublishAttempt {
  status: "publishing" | "completed";
  jobId: string;
  projectId: string;
  claims: FlowClaim[];
  existing: Array<[string, string]>;
  result?: PublishResult;
}

/** Read a durable publish attempt, if a prior process stopped mid-job. */
export async function loadPublishAttempt(stateDir: string, jobId: string): Promise<PublishAttempt | null> {
  const file = path.join(stateDir, "attempts", `${safeJobId(jobId)}.json`);
  try { return JSON.parse(await readFile(file, "utf8")) as PublishAttempt; } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

/** Render accepted claims, write their narrow source records, and promote pages. */
export async function publishClaims(
  wikiRoot: string,
  stateDir: string,
  job: FlowJob,
  claims: FlowClaim[],
  existing: ReadonlyMap<string, string>,
): Promise<PublishResult> {
  const locked = await acquireLock(wikiRoot);
  if (!locked) throw new Error("could not acquire wiki lock for knowledge publish");
  try {
    await replayJournal(wikiRoot);
    return await publishClaimsLocked(wikiRoot, stateDir, job, claims, existing);
  } finally {
    await releaseLock(wikiRoot);
  }
}

async function publishClaimsLocked(
  wikiRoot: string,
  stateDir: string,
  job: FlowJob,
  claims: FlowClaim[],
  existing: ReadonlyMap<string, string>,
): Promise<PublishResult> {
  const attempt = await loadPublishAttempt(stateDir, job.id);
  if (attempt?.status === "completed" && attempt.result) return attempt.result;
  const stableClaims = attempt?.claims ?? claims;
  const stableExisting = new Map(attempt?.existing ?? [...existing]);
  await writeAttempt(stateDir, job, stableClaims, stableExisting);
  const pending = await listCandidates(wikiRoot);
  const prepared = await preparePages(wikiRoot, job, stableClaims, stableExisting, pending);
  const approved = await approvePreparedPages(wikiRoot, stateDir, job, prepared.pages, stableExisting);
  const result = buildPublishResult(approved, prepared.blockedClaims);
  await completeAttempt(stateDir, job, stableClaims, stableExisting, result);
  return result;
}

interface PreparedPage { pageId: string; candidateId: string; body?: string; oldBody?: string; }
interface PreparedPages { pages: PreparedPage[]; blockedClaims: FlowClaim[]; }

async function preparePages(wikiRoot: string, job: FlowJob, claims: FlowClaim[], existing: ReadonlyMap<string, string>, pending: ReviewCandidate[]): Promise<PreparedPages> {
  const counts = countTargets(job, claims);
  const pages: PreparedPage[] = [];
  const blockedClaims: FlowClaim[] = [];
  for (const claim of claims) {
    const prepared = await prepareClaim(wikiRoot, job, claim, existing, counts, pending);
    if (prepared.page) pages.push(prepared.page);
    if (prepared.blocked) blockedClaims.push(prepared.blocked);
  }
  return { pages, blockedClaims };
}

async function prepareClaim(
  wikiRoot: string,
  job: FlowJob,
  claim: FlowClaim,
  existing: ReadonlyMap<string, string>,
  counts: ReadonlyMap<string, number>,
  pending: ReviewCandidate[],
): Promise<{ page?: PreparedPage; blocked?: FlowClaim }> {
  if (!validClaimTarget(job, claim, counts)) return { blocked: claim };
  const pageId = claim.targetPageId ?? `concepts/${scopedSlug(job.projectId, claim.slug)}`;
  const oldBody = claim.targetPageId ? existing.get(claim.targetPageId) : undefined;
  const currentBody = await readPage(wikiRoot, pageId);
  const sourcePath = sourceRelativePath(job, claim);
  const body = renderPage(job, claim, sourcePath, oldBody);
  const state = classifyPageState(claim, oldBody, currentBody, body);
  if (state === "blocked") return { blocked: claim };
  const pendingResult = reusePendingCandidate(claim, pageId, body, oldBody, pending);
  if (pendingResult) return pendingResult;
  if (state === "already") return { page: { pageId, candidateId: "", body, oldBody } };
  return createCandidatePage(wikiRoot, job, claim, pageId, sourcePath, body, oldBody);
}

function classifyPageState(
  claim: FlowClaim,
  oldBody: string | undefined,
  currentBody: string | null,
  body: string,
): "ready" | "already" | "blocked" {
  if (pageAlreadyMatches(currentBody, body)) return "already";
  if (existingTargetChanged(claim, oldBody, currentBody)) return "blocked";
  if (unexpectedNewPage(claim, currentBody)) return "blocked";
  if (!pageWithinLimit(oldBody, body)) return "blocked";
  return "ready";
}

function pageAlreadyMatches(currentBody: string | null, body: string): boolean {
  return currentBody !== null && currentBody === body;
}

function existingTargetChanged(claim: FlowClaim, oldBody: string | undefined, currentBody: string | null): boolean {
  return Boolean(claim.targetPageId && (!oldBody || targetChanged(oldBody, currentBody)));
}

function unexpectedNewPage(claim: FlowClaim, currentBody: string | null): boolean {
  return !claim.targetPageId && currentBody !== null;
}

function pageWithinLimit(oldBody: string | undefined, body: string): boolean {
  return (oldBody?.length ?? 0) <= MAX_EXISTING_PAGE_CHARS && body.length <= MAX_EXISTING_PAGE_CHARS;
}

function reusePendingCandidate(
  claim: FlowClaim,
  pageId: string,
  body: string,
  oldBody: string | undefined,
  pending: ReviewCandidate[],
): { page?: PreparedPage; blocked?: FlowClaim } | null {
  const prior = pending.find((candidate) => candidate.slug === pageId.split("/", 2)[1] && !candidate.targetEntityType && candidate.targetDirectory !== "queries");
  if (!prior) return null;
  return prior.body === body ? { page: { pageId, candidateId: prior.id, body, oldBody } } : { blocked: claim };
}

async function createCandidatePage(
  wikiRoot: string,
  job: FlowJob,
  claim: FlowClaim,
  pageId: string,
  sourcePath: string,
  body: string,
  oldBody: string | undefined,
): Promise<{ page?: PreparedPage; blocked?: FlowClaim }> {
  const evidence = job.evidence.find((item) => item.id === claim.evidenceId);
  if (!evidence) return { blocked: claim };
  const source = await writeEvidenceSource(wikiRoot, job, claim, evidence, sourcePath);
  const candidate = await writePageCandidate(wikiRoot, claim, pageId, source, body, job);
  return { page: { pageId, candidateId: candidate.id, body, oldBody } };
}

function validClaimTarget(job: FlowJob, claim: FlowClaim, counts: ReadonlyMap<string, number>): boolean {
  if (!isSafeFilenameComponent(claim.slug)) return false;
  if (claim.targetPageId && !job.allowedPageIds.includes(claim.targetPageId)) return false;
  const pageId = claim.targetPageId ?? `concepts/${scopedSlug(job.projectId, claim.slug)}`;
  return (counts.get(pageId) ?? 0) === 1;
}

function targetChanged(oldBody: string | undefined, currentBody: string | null): boolean {
  return !oldBody || currentBody !== oldBody;
}

function countTargets(job: FlowJob, claims: FlowClaim[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const claim of claims) {
    const pageId = claim.targetPageId ?? `concepts/${scopedSlug(job.projectId, claim.slug)}`;
    counts.set(pageId, (counts.get(pageId) ?? 0) + 1);
  }
  return counts;
}

function scopedSlug(projectId: string, slug: string): string {
  const readable = projectId.replace(/[^A-Za-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 32) || "project";
  return `${readable}-${sha256Text(projectId).slice(0, 8)}-${slug}`;
}

async function writeEvidenceSource(
  wikiRoot: string,
  job: FlowJob,
  claim: FlowClaim,
  evidence: FlowEvidence,
  relativePath = sourceRelativePath(job, claim),
): Promise<{ relativePath: string; hash: string }> {
  const content = `# Evidence\n\n${claim.quote}\n\n- Locator: ${evidence.locator}\n- Observed at: ${evidence.observedAt}\n- Evidence kind: ${evidence.kind}\n- Original evidence SHA-256: ${evidence.originalSha256 ?? evidence.sha256}\n`;
  await atomicWrite(path.join(wikiRoot, "sources", relativePath), content, { confineRoot: wikiRoot });
  return { relativePath, hash: sha256Text(content) };
}

async function writePageCandidate(
  wikiRoot: string,
  claim: FlowClaim,
  pageId: string,
  source: { relativePath: string; hash: string },
  body: string,
  job: FlowJob,
): Promise<Awaited<ReturnType<typeof writeCandidate>>> {
  const slug = pageId.split("/", 2)[1] ?? claim.slug;
  return writeCandidate(wikiRoot, {
    title: claim.title,
    slug,
    summary: claim.text.slice(0, 240),
    sources: [source.relativePath],
    body,
    sourceStates: { [source.relativePath]: { hash: source.hash, concepts: [slug], compiledAt: job.createdAt } },
    reviewMode: "policy",
    heldReasons: [{ code: "manual-review-requested" }],
  });
}

function sourceRelativePath(job: FlowJob, claim: FlowClaim): string {
  return `knowledge-flow-${sourceName(job.id, claim.slug, claim.text)}.md`;
}

function sourceName(jobId: string, slug: string, text: string): string {
  const digest = sha256Text(jobId).slice(0, 16);
  const claimDigest = sha256Text(`${slug}\n${text}`).slice(0, 10);
  return `${digest}-${claimDigest}-${slug.replace(/[^A-Za-z0-9-]/g, "-").slice(0, 50) || "claim"}`;
}

function renderPage(job: FlowJob, claim: FlowClaim, source: string, oldBody?: string): string {
  const date = job.createdAt || new Date().toISOString();
  const citation = `^[${source}:3${claim.quote.includes("\n") ? `-${2 + claim.quote.split("\n").length}` : ""}]`;
  if (oldBody) return appendCorrection(oldBody, job, claim, citation, date);
  const frontmatter = buildFrontmatter({
    title: claim.title,
    summary: claim.text.slice(0, 240),
    sources: [source],
    projectId: job.projectId,
    projectLabel: job.projectLabel,
    status: claim.status,
    createdAt: date,
    updatedAt: date,
    knowledgeClaimIds: [claimIdentity(job.projectId, claim)],
  });
  return `${frontmatter}\n\n## 结论\n\n${claim.text} ${citation}\n\n## 何时使用\n\n${claim.useWhen}\n\n## 记录理由\n\n${claim.rationale}\n`;
}

function appendCorrection(oldBody: string, job: FlowJob, claim: FlowClaim, citation: string, date: string): string {
  const parsed = parseFrontmatter(oldBody);
  const oldSources = Array.isArray(parsed.meta.sources) ? parsed.meta.sources.filter((item): item is string => typeof item === "string") : [];
  const ids = Array.isArray(parsed.meta.knowledgeClaimIds) ? parsed.meta.knowledgeClaimIds : [];
  const metadata = { ...parsed.meta, projectId: job.projectId, projectLabel: job.projectLabel, status: claim.status, updatedAt: date,
    knowledgeClaimIds: [...new Set([...ids, claimIdentity(job.projectId, claim)])],
    sources: [...new Set([...oldSources, citation.slice(2, -1).split(":", 1)[0]])] };
  const frontmatter = buildFrontmatter(metadata);
  const separator = parsed.body.endsWith("\n") ? "" : "\n";
  return `${frontmatter}\n\n${parsed.body}${separator}\n## ${date.slice(0, 10)} 更新\n\n${claim.text} ${citation}\n\n使用条件：${claim.useWhen}\n理由：${claim.rationale}\n`;
}

async function approveCandidate(wikiRoot: string, candidateId: string, expectedBody: string): Promise<void> {
  const candidate = await readCandidate(wikiRoot, candidateId);
  if (!candidate || candidate.body !== expectedBody) throw new Error("candidate changed before locked approval");
  await approveUnderLock(wikiRoot, candidateId, {});
  if (await readCandidate(wikiRoot, candidateId)) throw new Error("approved candidate was not cleared");
}

async function approvePreparedPages(
  wikiRoot: string,
  stateDir: string,
  job: FlowJob,
  pages: PreparedPage[],
  existing: ReadonlyMap<string, string>,
): Promise<PreparedPage[]> {
  const approved: PreparedPage[] = [];
  for (const page of pages) {
    await archivePage(stateDir, job, page, existing);
    if (page.candidateId) await approveCandidate(wikiRoot, page.candidateId, page.body ?? "");
    if (!await pageMatchesExpected(wikiRoot, page)) throw new Error("published page differs from approved body");
    approved.push(page);
  }
  return approved;
}

async function pageMatchesExpected(wikiRoot: string, page: PreparedPage): Promise<boolean> {
  const current = await readPage(wikiRoot, page.pageId);
  return current !== null && page.body !== undefined && current === page.body;
}

async function archivePage(stateDir: string, job: FlowJob, page: PreparedPage, existing: ReadonlyMap<string, string>): Promise<void> {
  const old = page.oldBody ?? existing.get(page.pageId);
  if (!old) return;
  const pagePart = page.pageId.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 80) || "page";
  const pageHash = sha256Text(page.pageId).slice(0, 10);
  const target = path.join(stateDir, "history", safeJobId(job.id), `${pagePart}-${pageHash}.md`);
  await mkdir(path.dirname(target), { recursive: true });
  await atomicWrite(target, old, { confineRoot: stateDir });
}

async function readPage(wikiRoot: string, pageId: string): Promise<string | null> {
  const file = await confineUnderRoot(path.join("wiki", `${pageId}.md`), wikiRoot, { mustExist: false });
  try { return await readFile(file, "utf8"); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

function safeJobId(id: string): string {
  return id.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 120) || "job";
}

async function writeAttempt(stateDir: string, job: FlowJob, claims: FlowClaim[], existing: ReadonlyMap<string, string>): Promise<void> {
  const file = path.join(stateDir, "attempts", `${safeJobId(job.id)}.json`);
  await mkdir(path.dirname(file), { recursive: true });
  const attempt: PublishAttempt = { status: "publishing", jobId: job.id, projectId: job.projectId, claims, existing: [...existing] };
  await atomicWrite(file, JSON.stringify(attempt, null, 2), { confineRoot: stateDir });
}

async function completeAttempt(stateDir: string, job: FlowJob, claims: FlowClaim[], existing: ReadonlyMap<string, string>, result: PublishResult): Promise<void> {
  const file = path.join(stateDir, "attempts", `${safeJobId(job.id)}.json`);
  const attempt: PublishAttempt = { status: "completed", jobId: job.id, projectId: job.projectId, claims, existing: [...existing], result };
  await atomicWrite(file, JSON.stringify(attempt, null, 2), { confineRoot: stateDir });
}

function buildPublishResult(approved: PreparedPage[], blockedClaims: FlowClaim[]): PublishResult {
  return { pageIds: approved.map((item) => item.pageId), candidateIds: approved.map((item) => item.candidateId).filter(Boolean), blockedClaims };
}
