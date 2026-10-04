/** Validate whole-topic edits against frozen destinations and exact source evidence before independent review. */
import { sha256Text } from "../../src/connectors/hash.js";
import { diagnoseClaims, formatClaimDiagnostics } from "./extract.js";
import type { FlowClaim, FlowEvidence, FlowJob } from "./types.js";
import { MAX_TOPIC_BODY_CHARS } from "./consolidation-plan.js";
import type { PlannedPage } from "./consolidation-plan.js";
import type { TopicRevision } from "./topic-revision-types.js";
import { validateCitationChanges, validateRetirementReferences } from "./citation-retirement.js";
import type { CitationRetirement } from "./citation-retirement.js";
import { resolveQuote } from "./consolidation-quotes.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";
import { unexpandedPlaceholder } from "./kept-paragraphs.js";

export interface TopicDraft {
  claims: FlowClaim[];
  pages: Array<{ pageId: string; body: string; claimIndexes: number[]; citationRetirements?: CitationRetirement[] }>;
  summary: string;
}

/** Correction output chooses IDs; source text and destination identity are restored after model output. */
export type CorrectionClaim = Omit<FlowClaim, "evidenceId" | "quote" | "topic" | "decisionObject" | "supportingQuotes"> & {
  quoteId: string;
  supportingQuotes: Array<{ quoteId: string }>;
};

export interface CorrectionTopicDraft {
  claims: CorrectionClaim[];
  pages: TopicDraft["pages"];
  summary: string;
}

/** Restore exact evidence and canonical destination metadata before normal validation. */
export function resolveCorrectionDraft(draft: CorrectionTopicDraft, catalog: readonly CorrectionEvidence[],
  frozenPages: readonly PlannedPage[]): TopicDraft {
  return { ...draft, claims: draft.claims.map(claim => resolveCorrectionClaim(claim, catalog, frozenPages)) };
}

function resolveCorrectionClaim(claim: CorrectionClaim, catalog: readonly CorrectionEvidence[],
  frozenPages: readonly PlannedPage[]): FlowClaim {
  const page = frozenPages.find(item => item.pageId === claim.targetPageId);
  if (!page) throw new Error(`unknown correction target page: ${claim.targetPageId ?? "null"}`);
  const primary = resolveQuote(catalog, claim.quoteId);
  const supportingQuotes = claim.supportingQuotes.map(item => {
    const option = resolveQuote(catalog, item.quoteId);
    return { evidenceId: option.evidenceId, quote: option.quote };
  });
  const { quoteId: _quoteId, supportingQuotes: _supporting, ...fields } = claim;
  return { ...fields, topic: page.topic, decisionObject: page.decisionObject,
    evidenceId: primary.evidenceId, quote: primary.quote, supportingQuotes };
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

/**
 * Evidence role decides authority, so a mislabeled claim is corrected instead of holding the whole page.
 * Assistant-primary claims become historical lessons, artifact-primary decisions become historical facts,
 * and assistant supporting quotes stay only on user-primary claims. Text, quotes and targets never change,
 * and claims the model marked uncertain stay held for review.
 */
export function withRoleAuthority(draft: TopicDraft, evidence: readonly FlowEvidence[]): TopicDraft {
  const roles = new Map(evidence.map(item => [item.id, item.kind]));
  return { ...draft, claims: draft.claims.map(claim => authorityFor(claim, roles)) };
}

function authorityFor(claim: FlowClaim, roles: ReadonlyMap<string, FlowEvidence["kind"]>): FlowClaim {
  const primary = roles.get(claim.evidenceId);
  if (claim.status === "uncertain" || primary === undefined) return claim;
  return supportFor(kindFor(claim, primary), primary, roles);
}

/** The primary quote's speaker bounds what kind of knowledge the claim may state. */
function kindFor(claim: FlowClaim, primary: FlowEvidence["kind"]): FlowClaim {
  if (primary === "assistant") return { ...claim, kind: "lesson", status: "historical" };
  if (primary === "artifact" && claim.kind === "decision") return { ...claim, kind: "fact", status: "historical" };
  return claim;
}

/** Assistant context may explain a user's own statement, but cannot lend authority to anything else. */
function supportFor(claim: FlowClaim, primary: FlowEvidence["kind"], roles: ReadonlyMap<string, FlowEvidence["kind"]>): FlowClaim {
  if (primary === "user" || !claim.supportingQuotes) return claim;
  return { ...claim, supportingQuotes: claim.supportingQuotes.filter(item => roles.get(item.evidenceId) !== "assistant") };
}

/** Every proposed claim belongs to exactly one planned whole-page edit. */
export function validatedDraft(draft: TopicDraft, job: FlowJob, pages: PlannedPage[], maximum: number,
  priorSources: Record<string, string> = {}): {
  claims: FlowClaim[]; revisions: TopicRevision[];
} {
  const allowed = pages.map(page => page.pageId);
  const diagnostics = diagnoseClaims(draft.claims, job.evidence, allowed, maximum);
  const claims = diagnostics.claims;
  if (diagnostics.diagnostics.length || !claims.length || claims.length !== draft.claims.length) {
    const detail = formatClaimDiagnostics(diagnostics.diagnostics) || "no accepted claims";
    throw new Error(`topic draft claim validation failed: ${detail}`);
  }
  if (claims.some(claim => claim.status === "uncertain")) throw new Error("topic draft claim validation failed: uncertain claims remain held");
  if (claims.some(claim => claim.kind === "decision" && job.evidence.find(item => item.id === claim.evidenceId)?.kind !== "user")) {
    throw new Error("reference material is not evidence of a user decision");
  }
  if (draft.pages.length !== pages.length) throw new Error("topic draft omitted a planned page");
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

function validateEdit(edit: TopicDraft["pages"][number], page: PlannedPage, claims: FlowClaim[], seen: Set<number>): void {
  if (!edit.claimIndexes.length || edit.body.trimStart().startsWith("---") || edit.body.length > MAX_TOPIC_BODY_CHARS) throw new Error("invalid topic body");
  for (const index of edit.claimIndexes) {
    const claim = claims[index];
    if (seen.has(index) || !belongsToPage(claim, page)) throw new Error("claim topic ownership mismatch");
    if (!edit.body.includes(`{{claim:${index}}}`)) throw new Error("topic body omitted claim citation");
    seen.add(index);
  }
  validateCitationMarkers(edit, page.original);
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
  validateCitationChanges([original ?? ""], edit.body, edit.citationRetirements, { claimIndexes: edit.claimIndexes });
}
