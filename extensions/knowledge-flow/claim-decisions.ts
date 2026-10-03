/**
 * Record the reviewer's per-claim conclusions without letting them decide anything yet.
 *
 * Phase B (deployment/KNOWLEDGE-LEDGER.md §7.2) will let accepted claims of a held batch
 * publish as a ledger record. Until that gate exists and is enabled, the page-level review
 * still decides every outcome; each review attempt's per-claim conclusions are only kept on
 * the result so the observation period can count how many held batches would have published
 * part of their claims. A missing or malformed list is recorded as incomplete, never held.
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

/** Attach the recorded attempts to the batch result, leaving results without a review untouched. */
export function withClaimReviews(result: FlowResult, reviews: readonly ClaimReview[]): FlowResult {
  return reviews.length ? { ...result, claimReviews: [...reviews] } : result;
}
