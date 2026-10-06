/**
 * Review continuity is program-owned context, never evidence or approval. Stable identities describe
 * which claims survived correction; paragraph deltas highlight changes while the reviewer still sees
 * complete originals, sources and proposed revisions for independent validation.
 */
import type { StableClaimEntry, TopicDraft } from "./consolidation-draft.js";
import type { ClaimDecision } from "./types.js";
import { pageParagraphs } from "./kept-paragraphs.js";

export interface TopicReview {
  decision: "accept" | "reject" | "needs_review";
  reason: string;
  checkedClaimIndexes: number[];
  checkedPageIds: string[];
  checkedRetiredCitations?: string[];
  claimDecisions?: ClaimDecision[];
  quoteRepairs?: Array<{ claimIndex: number; quoteId: string }>;
  replaceEvidenceForClaims?: number[];
}

export interface PreviousReview {
  mode: "correction" | "accepted_subset";
  draft: TopicDraft;
  stableClaims: StableClaimEntry[];
  review?: TopicReview;
}

/** Map the immediately preceding draft to the current one without relying on shifted numeric indexes. */
export function reviewContext(draft: TopicDraft, stableClaims: readonly StableClaimEntry[], previous?: PreviousReview) {
  if (!previous) return { mode: "initial" as const };
  const currentIndexes = new Map(stableClaims.map((entry, index) => [entry.claimId, index]));
  return { mode: previous.mode, previousReview: previous.review,
    claimMapping: previous.stableClaims.map((entry, previousClaimIndex) => ({ claimId: entry.claimId,
      previousClaimIndex, claimIndex: currentIndexes.get(entry.claimId) ?? null })),
    changedPages: changedPages(previous, draft, stableClaims) };
}

/** Use stable markers in paragraph comparisons so index compaction alone does not look like a rewrite. */
function changedPages(previous: PreviousReview, draft: TopicDraft, stableClaims: readonly StableClaimEntry[]) {
  const before = new Map(previous.draft.pages.map(page => [page.pageId, stableParagraphs(page.body, previous.stableClaims)]));
  const after = new Map(draft.pages.map(page => [page.pageId, stableParagraphs(page.body, stableClaims)]));
  return [...new Set([...before.keys(), ...after.keys()])].map(pageId => ({ pageId,
    removedParagraphs: difference(before.get(pageId) ?? [], after.get(pageId) ?? []),
    addedParagraphs: difference(after.get(pageId) ?? [], before.get(pageId) ?? []) }));
}

/** Keep repeated identical paragraphs visible when only one occurrence was removed. */
function difference(paragraphs: readonly string[], other: readonly string[]): string[] {
  const remaining = [...other];
  return paragraphs.filter(paragraph => {
    const index = remaining.indexOf(paragraph);
    if (index < 0) return true;
    remaining.splice(index, 1);
    return false;
  });
}

/** Stable IDs appear only in auxiliary review context; publishable drafts retain numeric markers. */
function stableParagraphs(body: string, claims: readonly StableClaimEntry[]): string[] {
  return pageParagraphs(body.replace(/\{\{claim:(\d+)\}\}/g, (marker, index: string) => {
    const claim = claims[Number(index)];
    return claim ? `{{claim:${claim.claimId}}}` : marker;
  }));
}
