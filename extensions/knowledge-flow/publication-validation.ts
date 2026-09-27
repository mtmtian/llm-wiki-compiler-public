/**
 * One evidence-authority boundary for legacy paragraphs and whole-page revisions.
 * A user may approve an assistant's proposal; the proposal is supporting context,
 * while assistant-primary claims remain dated analytical lessons. Shared packets
 * contain only their exact cited quotes, never an uncited transcript tail.
 */
import { validateClaims } from "./extract.js";
import type { FlowClaim, FlowEvidence } from "./types.js";
import type { PublicationRecord } from "./publication-types.js";

/** Reject unsupported or non-accepted packets before either replay path writes files. */
export function acceptedPublicationClaims(record: PublicationRecord, allowed: string[]): FlowClaim[] {
  const value = record.payload;
  const claims = validateClaims(value.claims, value.evidence, allowed, 5);
  const evidence = new Map(value.evidence.map(item => [item.id, item]));
  const used = new Set(value.claims.flatMap(claim => [claim.evidenceId,
    ...(claim.supportingQuotes ?? []).map(support => support.evidenceId)]));
  if (value.version !== 2 || value.review.status !== "accepted" || !claims.length
    || claims.length !== value.claims.length || evidence.size !== value.evidence.length
    || used.size !== evidence.size || [...used].some(id => !evidence.has(id))
    || value.claims.some(claim => !acceptedAuthority(claim, evidence))) {
    throw new Error("publication requires accepted claims with exact role-scoped evidence");
  }
  return claims;
}

/** Match the Python packet authority rule without downgrading invalid decided claims. */
function acceptedAuthority(claim: FlowClaim, evidence: ReadonlyMap<string, FlowEvidence>): boolean {
  const primary = evidence.get(claim.evidenceId);
  if (!primary || claim.quote !== primary.text || claim.status === "uncertain" || claim.replacementIntent) return false;
  if (!primaryAuthority(claim, primary.kind)) return false;
  return (claim.supportingQuotes ?? []).every(support => {
    const source = evidence.get(support.evidenceId);
    return source?.text === support.quote;
  });
}

/** Primary evidence determines authority; a support quote cannot promote it. */
function primaryAuthority(claim: FlowClaim, kind: FlowEvidence["kind"]): boolean {
  if (claim.status === "decided") return kind === "user";
  return kind !== "assistant" || claim.kind === "lesson" && claim.status === "historical";
}
