/** Validate whole-topic edits against frozen destinations and exact source evidence before independent review. */
import { sha256Text } from "../../src/connectors/hash.js";
import { authorityViolation } from "./authority-policy.js";
import { parseFrontmatter } from "../../src/utils/markdown.js";
import { diagnoseClaims, formatClaimDiagnostics } from "./extract.js";
import type { FlowClaim, FlowEvidence, FlowJob } from "./types.js";
import { TopicBodyLimitError } from "./consolidation-plan.js";
import { assertEvidenceDates } from "./consolidation-dates.js";
import type { PlannedPage } from "./consolidation-plan.js";
import type { TopicRevision } from "./topic-revision-types.js";
import { validateCitationChanges, validateRetirementReferences } from "./citation-retirement.js";
import type { CitationRetirement } from "./citation-retirement.js";
import { resolveQuote } from "./consolidation-quotes.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";
import { pageParagraphs, topicPageBodyBudget, unexpandedPlaceholder } from "./kept-paragraphs.js";

export interface TopicDraft {
  claims: FlowClaim[];
  pages: Array<{ pageId: string; body: string; claimIndexes: number[]; citationRetirements?: CitationRetirement[] }>;
  summary: string;
}

/** Valid evidence can still describe an undecided intent requiring human clarification. */
export class UncertainClaimsError extends Error {}

/** Model output chooses source IDs; source text and page metadata stay program-owned. */
export type QuoteBoundClaim = Omit<FlowClaim, "evidenceId" | "quote" | "topic" | "decisionObject" | "supportingQuotes"> & {
  quoteId: string;
  supportingQuotes: Array<{ quoteId: string }>;
};

export interface QuoteBoundTopicDraft {
  claims: QuoteBoundClaim[];
  pages: TopicDraft["pages"];
  summary: string;
}

/** Run-local claim identity and quote selectors never enter FlowClaim or publication output. */
export interface StableClaimEntry {
  claimId: string;
  claim: FlowClaim;
  quoteId: string;
  supportingQuoteIds: string[];
}

/** Restore exact source text and canonical page metadata from the frozen initial quote choices. */
export function resolveQuoteBoundDraft(draft: QuoteBoundTopicDraft, catalog: readonly CorrectionEvidence[],
  frozenPages: readonly PlannedPage[]): { draft: TopicDraft; stableClaims: StableClaimEntry[] } {
  const claims = draft.claims.map(claim => resolveBoundClaim(claim, catalog, frozenPages));
  const stableClaims = draft.claims.map((claim, index) => ({ claimId: `c${index}`, claim: claims[index],
    quoteId: claim.quoteId,
    supportingQuoteIds: normalizedSupportQuoteIds(claim.quoteId, claim.supportingQuotes.map(item => item.quoteId)) }));
  return { draft: { ...draft, claims }, stableClaims };
}

function resolveBoundClaim(claim: QuoteBoundClaim, catalog: readonly CorrectionEvidence[],
  frozenPages: readonly PlannedPage[]): FlowClaim {
  const page = frozenPages.find(item => item.pageId === claim.targetPageId);
  if (!page) throw new Error(`unknown correction target page: ${claim.targetPageId ?? "null"}`);
  const primary = resolveQuote(catalog, claim.quoteId);
  const supportingQuotes = normalizedSupportQuoteIds(claim.quoteId, claim.supportingQuotes.map(item => item.quoteId)).map(quoteId => {
    const option = resolveQuote(catalog, quoteId);
    return { evidenceId: option.evidenceId, quote: option.quote };
  });
  const { quoteId: _quoteId, supportingQuotes: _supporting, ...fields } = claim;
  return { ...fields, topic: page.topic, decisionObject: page.decisionObject,
    evidenceId: primary.evidenceId, quote: primary.quote, supportingQuotes };
}

export function normalizedSupportQuoteIds(primaryQuoteId: string, supportingQuoteIds: readonly string[]): string[] {
  return [...new Set(supportingQuoteIds)].filter(quoteId => quoteId !== primaryQuoteId);
}

/**
 * Summaries are never evidence. Retained context carries the original role, text and hash, and each
 * item is marked as evidence of the turns consolidated now ("current") or earlier session context.
 */
export function withSessionEvidence(job: FlowJob): FlowJob {
  const identity = (item: FlowEvidence) => JSON.stringify([item.id, item.kind, item.sha256, item.locator]);
  const current = new Set(job.evidence.map(identity));
  const originals = [...(job.sessionContext?.evidence ?? []), ...job.evidence];
  const unique = [...new Map(originals.map(item => [identity(item), item])).values()];
  if (unique.length > 500 || unique.reduce((total, item) => total + item.text.length, 0) > 200_000) {
    throw new Error("session evidence exceeds review budget; consume a smaller frozen batch");
  }
  const evidence = unique.map(item => ({
    ...(unique.some(other => other !== item && other.id === item.id)
      ? { ...item, id: `e-${sha256Text(JSON.stringify(item)).slice(0, 32)}` } : item),
    origin: current.has(identity(item)) ? "current" as const : "earlier" as const }));
  return { ...job, evidence };
}

/** Only complete, unchanged existing pages may enter independent no-change review. Nothing is published. */
export function unchangedRevisions(draft: TopicDraft, pages: readonly PlannedPage[]): TopicRevision[] | undefined {
  if (draft.claims.length || !pages.length || draft.pages.length !== pages.length) return undefined;
  if (new Set(draft.pages.map(edit => edit.pageId)).size !== pages.length) return undefined;
  const revisions: TopicRevision[] = [];
  for (const edit of draft.pages) {
    const page = pages.find(item => item.pageId === edit.pageId);
    if (!page || page.original === null || edit.claimIndexes.length || edit.citationRetirements?.length) return undefined;
    const { meta, body } = parseFrontmatter(page.original);
    if (edit.body.trim() !== body.trim() || page.title !== meta.title
      || page.topic !== meta.knowledgeTopic || page.decisionObject !== meta.knowledgeDecisionObject) return undefined;
    const { original: _original, ...identity } = page;
    revisions.push({ ...identity, body: edit.body, claimIndexes: [] });
  }
  return revisions;
}

/** Every proposed claim belongs to exactly one planned whole-page edit. */
export function validatedDraft(draft: TopicDraft, job: FlowJob, pages: PlannedPage[], maximum: number,
  priorSources: Record<string, string> = {}): {
  claims: FlowClaim[]; revisions: TopicRevision[];
} {
  const allowed = pages.map(page => page.pageId);
  assertExplicitAuthority(draft.claims, job.evidence);
  const diagnostics = diagnoseClaims(draft.claims, job.evidence, allowed, maximum);
  const claims = diagnostics.claims;
  if (diagnostics.diagnostics.length && diagnostics.diagnostics.every(item => item.code === "uncertain")
    && claims.length === draft.claims.length) throw new UncertainClaimsError("uncertain claims require confirmed user intent");
  if (diagnostics.diagnostics.length || !claims.length || claims.length !== draft.claims.length) {
    const detail = formatClaimDiagnostics(diagnostics.diagnostics) || "no accepted claims";
    throw new Error(`topic draft claim validation failed: ${detail}`);
  }
  if (claims.some(claim => claim.kind === "decision" && job.evidence.find(item => item.id === claim.evidenceId)?.kind !== "user")) {
    throw new Error("reference material is not evidence of a user decision");
  }
  if (draft.pages.length !== pages.length) throw new Error("topic draft omitted a planned page");
  assertEvidenceDates(draft, pages, job.evidence);
  const seen = new Set<number>(); const pageIds = new Set<string>();
  const revisions = draft.pages.map(edit => {
    const page = pages.find(item => item.pageId === edit.pageId);
    if (!page || pageIds.has(edit.pageId)) throw new Error("unplanned or duplicated topic page");
    pageIds.add(edit.pageId);
    validateEdit(edit, page, claims, seen);
    validateRetirementReferences(edit.citationRetirements ?? [], [...job.evidence.map(item => item.text),
      ...Object.values(priorSources), page.original ?? ""]);
    const { original: _original, ...identity } = page;
    return { ...identity, ...(job.topicScope === "semantic" ? { topicScope: "semantic" as const } : {}),
      body: edit.body, claimIndexes: edit.claimIndexes,
      ...(edit.citationRetirements?.length ? { citationRetirements: edit.citationRetirements } : {}) };
  });
  if (seen.size !== claims.length) throw new Error("topic draft omitted an accepted claim");
  return { claims, revisions };
}

function assertExplicitAuthority(claims: readonly FlowClaim[], evidence: readonly FlowEvidence[]): void {
  const roles = new Map(evidence.map(item => [item.id, item.kind]));
  for (const claim of claims) {
    const violation = authorityViolation(claim, roles.get(claim.evidenceId));
    if (violation) throw new Error(`topic draft claim validation failed: ${draftAuthorityMessages[violation]}`);
  }
}

const draftAuthorityMessages = {
  assistant: "assistant primary evidence supports only historical lessons",
  artifact: "artifact evidence supports only historical facts, lessons or constraints",
  decided: "decided claims require user-primary evidence",
} as const;

function validateEdit(edit: TopicDraft["pages"][number], page: PlannedPage, claims: FlowClaim[], seen: Set<number>): void {
  if (!edit.claimIndexes.length) throw new Error(`topic page ${page.pageId} has no claim indexes`);
  if (edit.body.trimStart().startsWith("---")) throw new Error(`topic page ${page.pageId} must not include frontmatter`);
  validateBodyBudget(page.pageId, parseFrontmatter(edit.body).body.length);
  for (const index of edit.claimIndexes) {
    const claim = claims[index];
    if (seen.has(index) || !belongsToPage(claim, page)) throw new Error("claim topic ownership mismatch");
    if (!edit.body.includes(`{{claim:${index}}}`)) throw new Error("topic body omitted claim citation");
    seen.add(index);
  }
  validateCitationMarkers(edit, page.original);
}

function validateBodyBudget(pageId: string, expandedBodyChars: number): void {
  const budget = topicPageBodyBudget(pageId, expandedBodyChars);
  if (budget.additionalAvailableChars >= 0) return;
  throw new TopicBodyLimitError(pageId, expandedBodyChars);
}

function belongsToPage(claim: FlowClaim | undefined, page: PlannedPage): boolean {
  return Boolean(claim && claim.targetPageId === page.pageId
    && claim.topic === page.topic && claim.decisionObject === page.decisionObject);
}

function validateCitationMarkers(edit: TopicDraft["pages"][number], original: string | null | undefined): void {
  for (const match of edit.body.matchAll(/\{\{claim:([^}]+)\}\}/g)) {
    if (!/^\d+$/.test(match[1]) || !edit.claimIndexes.includes(Number(match[1]))) throw new Error("unknown claim citation");
  }
  const placeholder = unexpandedPlaceholder(edit.body);
  if (placeholder) {
    throw new Error(`kept paragraph placeholder ${placeholder} was not expanded: write each keep placeholder alone on its own line, `
      + "and only in the body of the existing page that lists it");
  }
  validateClaimProse(edit.body);
  validateCitationChanges([original ?? ""], edit.body, edit.citationRetirements, { claimIndexes: edit.claimIndexes });
}

/** Claim placeholders render citations only; a title or list marker cannot stand in for page prose. */
function validateClaimProse(body: string): void {
  for (const paragraph of pageParagraphs(body)) {
    if (!/\{\{claim:\d+\}\}/.test(paragraph)) continue;
    const prose = paragraph.replace(/^ {0,3}#{1,6}(?:[\t ]+|$).*$/gm, "")
      .replace(/\{\{claim:\d+\}\}|\^\[[^\]\r\n]+\]/g, "")
      .replace(/^[\t ]*(?:[-+*]|\d+[.)])[\t ]+/gm, "");
    if (!/[\p{L}\p{N}]/u.test(prose)) {
      throw new Error("topic body claim citation has no accompanying prose; write the supported assertion in the same paragraph");
    }
  }
}
