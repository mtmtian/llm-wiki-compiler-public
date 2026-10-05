/** Adapt reviewed records to task candidates and keep superseded page conclusions out of current context. */
import { reviewedClaimRevision, type ReviewedClaim, type ReviewedClaimProjection } from "./ledger.js";
import type { TaskCandidate } from "./task-ranking.js";
import type { DecisionSection } from "./task-sections.js";
import type { LedgerTaskEvidence, TaskContextOptions } from "./task-types.js";

export type TaskSelection = { origin: "page"; section: DecisionSection } | { origin: "ledger"; claim: ReviewedClaim };

/** Superseded records remain available for explicit historical questions, not routine current work. */
function asksHistory(prompt: string): boolean {
  return /当时|以前|此前|历史|过去|原来|旧方案|\b(historical|previous|formerly)\b|used to/i.test(prompt);
}

/** Claim scope is independent of page inventory: a reviewed record may precede its first page. */
export function scopedClaims(projection: ReviewedClaimProjection | null, options: TaskContextOptions): ReviewedClaim[] {
  const claims = (projection?.claims ?? []).filter(claim => (options.scope === "semantic" || claim.projectId === options.projectId)
    && (!claim.superseded || asksHistory(options.prompt)));
  const distinct = new Map<string, ReviewedClaim>();
  for (const claim of claims) {
    const identity = JSON.stringify([claim.projectId, claim.topic, claim.decisionObject, claim.text,
      claim.kind, claim.status, claim.useWhen, claim.rationale, claim.superseded, claim.quotes]);
    if (!distinct.has(identity)) distinct.set(identity, claim);
  }
  return [...distinct.values()];
}

/** Source text and applicability stay separate so a generic condition cannot make an unrelated claim relevant. */
export function claimCandidate(claim: ReviewedClaim): TaskCandidate<TaskSelection> {
  return { id: claim.claimRef, title: claim.title, heading: claim.topic, text: claim.text, topic: claim.topic,
    decisionObject: claim.decisionObject, sourceProjectIds: [claim.projectId],
    temporalStatus: claim.superseded || claim.status === "historical" ? "historical" : "unspecified",
    semanticScore: 0, semanticAvailable: false, value: { origin: "ledger", claim } };
}

/** Quotes come directly from the reviewed publication; no source Markdown or live approval is inferred. */
export function claimEvidence(claim: ReviewedClaim): LedgerTaskEvidence {
  return { origin: "ledger", claimRef: claim.claimRef, recordId: claim.recordId, recordRevision: reviewedClaimRevision(claim),
    title: claim.title, updatedAt: claim.recordedAt || null, decisionObject: claim.decisionObject || null,
    section: claim.topic, text: claim.text, qualifications: [claim.useWhen, claim.rationale].filter(Boolean).join("\n"),
    claimKind: claim.kind, claimStatus: claim.status, quotes: claim.quotes, sources: [], sourceProjectIds: [claim.projectId],
    temporalStatus: claim.superseded || claim.status === "historical" ? "historical" : "unspecified" };
}

/** Unmappable old publications conservatively withhold the page until consolidation restores a clear boundary. */
export function withoutSupersededSections(sections: DecisionSection[], projection: ReviewedClaimProjection | null,
  prompt: string, warnings: string[]): DecisionSection[] {
  const retired = projection?.superseded ?? [];
  if (!retired.length) return sections;
  const historical = asksHistory(prompt);
  const output: DecisionSection[] = [];
  for (const section of sections) {
    const refs = section.page.frontmatter.knowledgePublicationRefs;
    const relevant = retired.filter(claim => Array.isArray(refs) && refs.includes(claim.claimRef));
    const ambiguous = relevant.some(claim => !plain(section.page.body).includes(plain(claim.text)));
    const affected = ambiguous || relevant.some(claim => plain(section.text + "\n" + (section.qualifications ?? "")).includes(plain(claim.text)));
    if (ambiguous && !warnings.includes("superseded-page-unmapped")) warnings.push("superseded-page-unmapped");
    if (!affected) output.push(section);
    else if (historical) output.push({ ...section, temporalStatus: "historical" });
  }
  return output;
}

/** Ignore rendered citation markers and harmless whitespace when binding old claims to a page section. */
function plain(value: string): string { return value.replace(/\^\[[^\]]*\]/g, "").replace(/\s+/g, "").normalize("NFKC"); }
