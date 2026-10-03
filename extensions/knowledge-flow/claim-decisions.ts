/**
 * Per-claim review conclusions and the ledger claims of a held batch.
 *
 * Every review attempt's per-claim conclusions are recorded on the result. The page-level
 * review still decides whether a batch publishes or is held. When the shared knowledge-ledger
 * gate is enabled (deployment/KNOWLEDGE-LEDGER.md §7.2), a held batch also carries the claims
 * its final review accepted, with only the evidence they cite, so the host can publish them
 * as one ledger record while the batch stays held. Ledger records never touch page text, so
 * they need no citation retirement. A missing or incomplete list publishes nothing.
 */
import type { ClaimDecision, ClaimReview, FlowResult } from "./types.js";

/** Summarize one review attempt; `complete` requires exactly one conclusion per claim index. */
export function claimReview(stage: string | undefined, review: { decision: ClaimDecision["decision"]; claimDecisions?: ClaimDecision[] },
  claimCount: number): ClaimReview {
  const claims = Array.isArray(review.claimDecisions) ? review.claimDecisions : [];
  const indexes = new Set(claims.map(item => item.claimIndex));
  const complete = claims.length === claimCount && indexes.size === claimCount
    && Array.from({ length: claimCount }, (_, index) => index).every(index => indexes.has(index));
  return { stage: stage === "correction" ? "correction" : "initial", decision: review.decision, complete, claims };
}

type Contribution = NonNullable<FlowResult["contribution"]>;

/**
 * Finish a batch result: record its review attempts and, when the ledger is enabled, let a held
 * batch carry the claims its final review accepted. Submitted and empty results are unchanged.
 */
export function finishResult(result: FlowResult, reviews: readonly ClaimReview[],
  ledger: { enabled: boolean; reviewed?: Contribution }): FlowResult {
  const recorded = reviews.length ? { ...result, claimReviews: [...reviews] } : result;
  const ledgerContribution = ledger.enabled && result.status === "needs_review" && ledger.reviewed
    ? acceptedClaims(ledger.reviewed, reviews.at(-1)) : undefined;
  if (!ledgerContribution) return recorded;
  const note = `其中 ${ledgerContribution.claims.length} 条已接受的 claim 提交为账本记录`;
  return { ...recorded, ledgerContribution, error: recorded.error ? `${recorded.error}；${note}` : note };
}

/** The accepted claims of a complete review, with only the evidence they cite. */
function acceptedClaims(contribution: Contribution, review: ClaimReview | undefined): FlowResult["ledgerContribution"] {
  if (!review?.complete) return undefined;
  const accepted = new Set(review.claims.filter(item => item.decision === "accept").map(item => item.claimIndex));
  const claims = contribution.claims.filter((_, index) => accepted.has(index));
  if (!claims.length) return undefined;
  const cited = new Set(claims.flatMap(claim => [claim.evidenceId, ...(claim.supportingQuotes ?? []).map(item => item.evidenceId)]));
  return { claims, evidence: contribution.evidence.filter(item => cited.has(item.id)) };
}
