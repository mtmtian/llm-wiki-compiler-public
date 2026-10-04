/** Apply only complete, exact primary-quote repairs proposed by a whole-page review. */
import type { FlowEvidence } from "./types.js";
import type { TopicDraft } from "./consolidation-draft.js";
import { resolveQuote } from "./consolidation-quotes.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";

interface ReviewRepair {
  decision: string;
  claimDecisions?: Array<{ claimIndex: number; decision: string }>;
  quoteRepairs?: Array<{ claimIndex: number; quoteId: string }>;
  retainEvidenceForClaims?: number[];
}

interface Replacement { evidenceId: string; quote: string; }

/** Return a copy with only cited primary references changed, or null when the advice is unsafe or incomplete. */
export function applyReviewedQuoteRepairs(draft: TopicDraft, review: ReviewRepair,
  catalog: readonly CorrectionEvidence[], evidence: readonly FlowEvidence[]): TopicDraft | null {
  if (review.decision !== "reject") return null;
  const disputed = rejectedClaimIndexes(review.claimDecisions, draft.claims.length);
  const targets = suggestionTargets(review.quoteRepairs, disputed);
  if (!targets) return null;
  if (review.retainEvidenceForClaims?.some(index => targets.has(index))) return null;
  const replacements = replacementMap(targets, draft, catalog, evidence);
  if (!replacements) return null;
  return { ...draft, claims: draft.claims.map((claim, index) => {
    const replacement = replacements.get(index);
    return replacement ? { ...claim, ...replacement } : claim;
  }) };
}

/** Require one valid per-claim outcome for every frozen claim. */
function rejectedClaimIndexes(decisions: ReviewRepair["claimDecisions"], claimCount: number): Set<number> | null {
  if (!Array.isArray(decisions) || decisions.length !== claimCount) return null;
  const seen = new Set<number>();
  for (const item of decisions) {
    if (!validDecision(item, claimCount, seen)) return null;
    seen.add(item.claimIndex);
  }
  return new Set(decisions.filter(item => item.decision !== "accept").map(item => item.claimIndex));
}

function validDecision(item: { claimIndex: number; decision: string }, claimCount: number, seen: Set<number>): boolean {
  const validIndex = Number.isInteger(item.claimIndex) && item.claimIndex >= 0 && item.claimIndex < claimCount;
  const validOutcome = ["accept", "reject", "needs_review"].includes(item.decision);
  return validIndex && validOutcome && !seen.has(item.claimIndex);
}

/** Require exact one-to-one coverage of every non-accepted claim and no accepted claim. */
function suggestionTargets(suggestions: ReviewRepair["quoteRepairs"], disputed: Set<number> | null): Map<number, string> | null {
  if (!disputed?.size || !Array.isArray(suggestions) || suggestions.length !== disputed.size) return null;
  const targets = new Map<number, string>();
  for (const suggestion of suggestions) {
    if (!disputed.has(suggestion.claimIndex) || targets.has(suggestion.claimIndex)) return null;
    targets.set(suggestion.claimIndex, suggestion.quoteId);
  }
  return targets.size === disputed.size ? targets : null;
}

function replacementMap(targets: Map<number, string>, draft: TopicDraft,
  catalog: readonly CorrectionEvidence[], evidence: readonly FlowEvidence[]): Map<number, Replacement> | null {
  const byId = new Map(evidence.map(item => [item.id, item]));
  const replacements = new Map<number, Replacement>();
  for (const [index, quoteId] of targets) {
    const replacement = replacementForClaim(index, quoteId, draft, catalog, byId);
    if (!replacement) return null;
    replacements.set(index, replacement);
  }
  return replacements.size === targets.size ? replacements : null;
}

function replacementForClaim(index: number, quoteId: string, draft: TopicDraft,
  catalog: readonly CorrectionEvidence[], evidenceById: ReadonlyMap<string, FlowEvidence>): Replacement | null {
  const previous = draft.claims[index];
  const source = evidenceById.get(previous.evidenceId);
  const replacement = tryResolveQuote(catalog, quoteId);
  if (!source || !replacement || samePrimary(previous, replacement)) return null;
  const nextSource = evidenceById.get(replacement.evidenceId);
  if (!nextSource || !canReplacePrimary(previous, source, nextSource, evidenceById)) return null;
  return replacement;
}

function tryResolveQuote(catalog: readonly CorrectionEvidence[], quoteId: string): Replacement | null {
  try { return resolveQuote(catalog, quoteId); }
  catch { return null; }
}

function samePrimary(claim: TopicDraft["claims"][number], replacement: Replacement): boolean {
  return claim.evidenceId === replacement.evidenceId && claim.quote === replacement.quote;
}

/** Prevent a reference-only repair from increasing or changing the claim's authority. */
function canReplacePrimary(claim: TopicDraft["claims"][number], previous: FlowEvidence, next: FlowEvidence,
  evidenceById: ReadonlyMap<string, FlowEvidence>): boolean {
  return !increasesAuthority(previous, next) && preservesDecisionAuthority(claim, next)
    && assistantRoleIsAllowed(claim, next) && artifactRoleIsAllowed(claim, next)
    && preservesAssistantSupportRole(claim, next, evidenceById);
}

function increasesAuthority(previous: FlowEvidence, next: FlowEvidence): boolean {
  return authorityRank(next.kind) > authorityRank(previous.kind);
}

function preservesDecisionAuthority(claim: TopicDraft["claims"][number], next: FlowEvidence): boolean {
  return (claim.kind !== "decision" && claim.status !== "decided") || next.kind === "user";
}

function assistantRoleIsAllowed(claim: TopicDraft["claims"][number], next: FlowEvidence): boolean {
  return next.kind !== "assistant" || (claim.kind === "lesson" && claim.status === "historical");
}

function artifactRoleIsAllowed(claim: TopicDraft["claims"][number], next: FlowEvidence): boolean {
  return next.kind !== "artifact" || (claim.kind !== "decision" && claim.status === "historical");
}

function preservesAssistantSupportRole(claim: TopicDraft["claims"][number], next: FlowEvidence,
  evidenceById: ReadonlyMap<string, FlowEvidence>): boolean {
  if (next.kind === "user") return true;
  return !claim.supportingQuotes?.some(item => evidenceById.get(item.evidenceId)?.kind === "assistant");
}

/** User evidence can authorize decisions; artifact and assistant evidence have narrower roles. */
function authorityRank(kind: FlowEvidence["kind"]): number {
  return kind === "user" ? 2 : kind === "artifact" ? 1 : 0;
}
