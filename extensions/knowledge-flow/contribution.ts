/** Shared quote narrowing for frozen legacy jobs and session topic revisions. */
import { sha256Text } from "../../src/connectors/hash.js";
import type { FlowClaim, FlowEvidence, FlowJob, FlowResult } from "./types.js";
import type { TopicRevision } from "./topic-revision-types.js";

/** Keep only cited exact quotes in the shared record; the local checkpoint retains conversation context. */
export function quoteContribution(job: FlowJob, claims: FlowClaim[], revisions?: TopicRevision[]): NonNullable<FlowResult["contribution"]> {
  const evidence: FlowEvidence[] = [];
  const narrow = (reference: { evidenceId: string; quote: string }) => {
    // origin only guides the prompts; published evidence keeps the canonical shape.
    const { origin: _origin, ...source } = job.evidence.find(item => item.id === reference.evidenceId)!;
    const existing = evidence.find(item => item.text === reference.quote && item.locator === source.locator && item.kind === source.kind);
    if (existing) return { evidenceId: existing.id, quote: reference.quote };
    const item = { ...source, id: `quote-${evidence.length}`, text: reference.quote,
      originalSha256: source.originalSha256 ?? source.sha256, sha256: sha256Text(reference.quote) };
    evidence.push(item);
    return { evidenceId: item.id, quote: reference.quote };
  };
  const narrowed = claims.map(claim => ({ ...claim, ...narrow(claim), replacementIntent: false,
    ...(claim.supportingQuotes?.length ? { supportingQuotes: claim.supportingQuotes.map(narrow) } : {}) }));
  return { claims: narrowed, evidence, ...(revisions ? { topicRevisions: revisions } : {}) };
}
