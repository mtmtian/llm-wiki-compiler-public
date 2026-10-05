/**
 * Run-local stable identities for bounded whole-page corrections. Claim IDs let a correction update or
 * drop one reviewed proposal without regenerating its neighbors. They are compacted back to ordinary
 * numeric placeholders before validation and never enter FlowClaim or publication data.
 */
import type { LLMTool } from "../../src/utils/provider.js";
import { authorityViolation } from "./authority-policy.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";
import { resolveQuote } from "./consolidation-quotes.js";
import type { CitationRetirement } from "./citation-retirement.js";
import { normalizedSupportQuoteIds } from "./consolidation-draft.js";
import type { StableClaimEntry, TopicDraft } from "./consolidation-draft.js";
import type { FlowClaim, FlowEvidence } from "./types.js";
import type { PlannedPage } from "./consolidation-plan.js";
import { MAX_TOPIC_BODY_CHARS } from "./consolidation-plan.js";
import { schemaArray as array, schemaObject as object, schemaText as text } from "./schema-builders.js";
const CLAIM_ID = /^c\d+$/;
const ROLE_AUTHORITY: Record<FlowEvidence["kind"], number> = { assistant: 0, artifact: 1, user: 2 };
const CLAIM_UPDATE_FIELDS = new Set<ClaimFieldChange["field"]>([
  "text", "title", "slug", "targetPageId", "kind", "status", "useWhen", "rationale", "replacementIntent",
  "quoteId", "supportingQuoteIds",
]);

export type ClaimFieldChange =
  | { field: "text"; value: string }
  | { field: "title"; value: string }
  | { field: "slug"; value: string }
  | { field: "targetPageId"; value: string }
  | { field: "kind"; value: FlowClaim["kind"] }
  | { field: "status"; value: Exclude<FlowClaim["status"], "uncertain"> }
  | { field: "useWhen"; value: string }
  | { field: "rationale"; value: string }
  | { field: "replacementIntent"; value: boolean }
  | { field: "quoteId"; value: string }
  | { field: "supportingQuoteIds"; value: string[] };

export interface ClaimUpdate {
  claimId: string;
  changes: ClaimFieldChange[];
}

export interface CorrectionPatch {
  claimUpdates: ClaimUpdate[];
  droppedClaimIds: string[];
  pages: Array<{ pageId: string; body: string; claimIds: string[]; citationRetirements?: CitationRetirement[] }>;
  summary: string;
}

export interface CorrectionPermissions {
  lockedClaimIds: readonly string[];
  replaceEvidenceForClaimIds: readonly string[];
}

interface PriorClaimReview {
  claimDecisions?: Array<{ claimIndex: number; decision: "accept" | "reject" | "needs_review" }>;
  replaceEvidenceForClaims?: number[];
  quoteRepairs?: Array<{ claimIndex: number; quoteId: string }>;
}

/** Convert reviewer indexes; only one explicit non-accept verdict leaves a claim mutable. */
export function correctionPermissionsForReview(claims: readonly StableClaimEntry[], review: PriorClaimReview): CorrectionPermissions {
  const decisions = indexDecisions(review.claimDecisions, claims.length);
  const mutableIndexes = new Set(claims.flatMap((_item, index) => {
    const verdicts = decisions.get(index) ?? [];
    return verdicts.length === 1 && verdicts[0].decision !== "accept" ? [index] : [];
  }));
  const lockedClaimIds = claims.flatMap((item, index) => mutableIndexes.has(index) ? [] : [item.claimId]);
  const replacementIndexes = [...(review.replaceEvidenceForClaims ?? []), ...(review.quoteRepairs ?? []).map(item => item.claimIndex)];
  if (hasDuplicate(review.replaceEvidenceForClaims ?? []) || hasDuplicate((review.quoteRepairs ?? []).map(item => item.claimIndex))) {
    throw new Error("evidence replacement permission contains duplicate claim indexes");
  }
  const replacementIds = new Set<string>();
  for (const index of replacementIndexes) {
    if (!Number.isInteger(index) || !mutableIndexes.has(index) || !claims[index]) {
      throw new Error("evidence replacement permission must name one non-accepted claim");
    }
    replacementIds.add(claims[index].claimId);
  }
  return { lockedClaimIds, replaceEvidenceForClaimIds: [...replacementIds] };
}

function indexDecisions(decisions: PriorClaimReview["claimDecisions"], count: number) {
  const indexed = new Map<number, NonNullable<PriorClaimReview["claimDecisions"]>>();
  if (!Array.isArray(decisions)) return indexed;
  for (const item of decisions) {
    if (!Number.isInteger(item.claimIndex) || item.claimIndex < 0 || item.claimIndex >= count
      || !["accept", "reject", "needs_review"].includes(item.decision)) {
      throw new Error("claim decision indexes must match the reviewed claims");
    }
    indexed.set(item.claimIndex, [...(indexed.get(item.claimIndex) ?? []), item]);
  }
  return indexed;
}

function hasDuplicate(values: readonly number[]): boolean {
  return new Set(values).size !== values.length;
}

/** Render the prior draft with stable claim references for the correction request. */
export function stableDraftView(draft: TopicDraft, claims: readonly StableClaimEntry[]) {
  const byIndex = new Map(claims.map((item, index) => [index, item.claimId]));
  return { summary: draft.summary, claims: claims.map(entry => claimView(entry)),
    pages: draft.pages.map(page => ({ ...page, claimIds: page.claimIndexes.flatMap(index => {
      const claimId = byIndex.get(index);
      return claimId ? [claimId] : [];
    }), body: stableBody(page.body, byIndex),
    ...(page.citationRetirements ? { citationRetirements: page.citationRetirements.map(item => ({ ...item,
      replacement: stableReplacement(item.replacement, byIndex) })) } : {}) })) };
}

function claimView(entry: StableClaimEntry) {
  const { evidenceId: _evidenceId, quote: _quote, topic: _topic, decisionObject: _decisionObject,
    supportingQuotes: _supportingQuotes, ...fields } = entry.claim;
  return { claimId: entry.claimId, ...fields, quoteId: entry.quoteId, supportingQuoteIds: entry.supportingQuoteIds };
}

function stableBody(body: string, byIndex: ReadonlyMap<number, string>): string {
  return body.replace(/\{\{claim:(\d+)\}\}/g, (marker, rawIndex: string) => {
    const claimId = byIndex.get(Number(rawIndex));
    return claimId ? `{{claim:${claimId}}}` : marker;
  });
}

function stableReplacement(replacement: string, byIndex: ReadonlyMap<number, string>): string {
  return replacement.replace(/^\{\{claim:(\d+)\}\}$/, (marker, rawIndex: string) => {
    const claimId = byIndex.get(Number(rawIndex));
    return claimId ? `{{claim:${claimId}}}` : marker;
  });
}

/** Build a strict patch schema with only mutable claims and reviewer-authorized evidence fields. */
export function createCorrectionPatchTool(destinationPageIds: readonly string[], catalog: readonly CorrectionEvidence[],
  claims: readonly StableClaimEntry[], permissions: CorrectionPermissions): LLMTool {
  const mutable = claims.filter(item => !permissions.lockedClaimIds.includes(item.claimId));
  const updates = mutable.map(item => updateBranch(item, destinationPageIds, catalog, permissions));
  const allowedIds = claims.map(item => item.claimId);
  const pages = pageSchema(destinationPageIds, allowedIds);
  const updateItems = updates.length ? { anyOf: updates } : emptyObjectSchema();
  return { name: "knowledge_topic_edit", description: "Patch only identified draft claims and rewrite every planned page.",
    input_schema: object({ claimUpdates: array(updateItems, mutable.length), droppedClaimIds: array(idSchema(mutable.map(item => item.claimId)), mutable.length),
      pages: array(pages, destinationPageIds.length), summary: text(4000) }) };
}

function updateBranch(entry: StableClaimEntry, destinationPageIds: readonly string[], catalog: readonly CorrectionEvidence[],
  permissions: CorrectionPermissions): Record<string, unknown> {
  const evidenceMayChange = permissions.replaceEvidenceForClaimIds.includes(entry.claimId);
  const oldRole = sourceRole(entry, catalog);
  const candidateRoles = permittedRoles(oldRole, evidenceMayChange);
  const fieldSchemas: Record<string, Record<string, unknown>> = {
    text: text(1200), title: text(160), slug: text(100), targetPageId: idSchema(destinationPageIds),
    kind: { enum: [...new Set(candidateRoles.flatMap(roleKinds))] },
    status: { enum: [...new Set(candidateRoles.flatMap(roleStatuses))] },
    useWhen: text(500), rationale: text(500), replacementIntent: { type: "boolean" },
  };
  if (evidenceMayChange) {
    fieldSchemas.quoteId = quoteIdSchema(catalog, oldRole);
    fieldSchemas.supportingQuoteIds = array(allowedQuoteIds(catalog, oldRole === "user"), 3);
  }
  const changes = Object.entries(fieldSchemas).map(([field, value]) => object({ field: { enum: [field] }, value }));
  return object({ claimId: { type: "string", enum: [entry.claimId] }, changes: {
    type: "array", minItems: 1, maxItems: changes.length, items: { anyOf: changes },
  } });
}

function pageSchema(pageIds: readonly string[], claimIds: readonly string[]): Record<string, unknown> {
  return object({ pageId: idSchema(pageIds), body: text(MAX_TOPIC_BODY_CHARS),
    claimIds: array(idSchema(claimIds), Math.min(5, claimIds.length)) },
    { citationRetirements: array(retirementSchema(claimIds), 500) });
}

function retirementSchema(claimIds: readonly string[]): Record<string, unknown> {
  const stableMarkers = claimIds.map(claimId => `{{claim:${claimId}}}`);
  const replacements = [
    { type: "string", minLength: 1, maxLength: 2048, pattern: "^\\^\\[[^\\]\\r\\n]+\\]$" },
    ...(stableMarkers.length ? [{ type: "string", enum: [...new Set(stableMarkers)] }] : []),
    { type: "string", minLength: 1, maxLength: 2048, pattern: "^https://[^\\s]+$" },
  ];
  return object({ citation: text(1024), reason: text(1000), replacement: { anyOf: replacements } });
}

function idSchema(ids: readonly string[]): Record<string, unknown> {
  return { type: "string", minLength: 1, ...(ids.length ? { enum: [...new Set(ids)] } : {}) };
}

function quoteIdSchema(catalog: readonly CorrectionEvidence[], currentRole: FlowEvidence["kind"]): Record<string, unknown> {
  const roles = new Set(permittedRoles(currentRole, true));
  return idSchema(catalog.flatMap(item => roles.has(item.kind) ? item.quoteOptions.map(option => option.quoteId) : []));
}

function allowedQuoteIds(catalog: readonly CorrectionEvidence[], allowAssistant: boolean): Record<string, unknown> {
  return idSchema(catalog.flatMap(item => allowAssistant || item.kind !== "assistant"
    ? item.quoteOptions.map(option => option.quoteId) : []));
}

function sourceRole(entry: StableClaimEntry, catalog: readonly CorrectionEvidence[]): FlowEvidence["kind"] {
  const role = catalog.find(item => item.id === entry.claim.evidenceId)?.kind;
  if (!role) throw new Error(`unknown frozen source for ${entry.claimId}`);
  return role;
}

function permittedRoles(current: FlowEvidence["kind"], mayChange: boolean): FlowEvidence["kind"][] {
  if (!mayChange) return [current];
  return (Object.keys(ROLE_AUTHORITY) as FlowEvidence["kind"][])
    .filter(role => ROLE_AUTHORITY[role] <= ROLE_AUTHORITY[current]);
}

function roleKinds(role: FlowEvidence["kind"]): FlowClaim["kind"][] {
  if (role === "assistant") return ["lesson"];
  if (role === "artifact") return ["fact", "lesson", "constraint"];
  return ["decision", "fact", "constraint", "lesson"];
}

function roleStatuses(role: FlowEvidence["kind"]): Exclude<FlowClaim["status"], "uncertain">[] {
  return role === "user" ? ["decided", "historical"] : ["historical"];
}

function emptyObjectSchema(): Record<string, unknown> {
  return { type: "object", additionalProperties: false, properties: {}, required: [] };
}

/** Apply a correction by stable identity, then compact IDs into the publication's ordinary indexes. */
export function applyCorrectionPatch(patch: CorrectionPatch, current: readonly StableClaimEntry[],
  permissions: CorrectionPermissions, catalog: readonly CorrectionEvidence[], frozenPages: readonly PlannedPage[]):
  { draft: TopicDraft; stableClaims: StableClaimEntry[] } {
  validatePatchShape(patch, current, permissions);
  const updates = new Map(patch.claimUpdates.map(update => [update.claimId, update]));
  const dropped = new Set(patch.droppedClaimIds);
  const nextClaims = current.filter(item => !dropped.has(item.claimId))
    .map(item => resolveEntryUpdate(item, updates.get(item.claimId), permissions, catalog, frozenPages));
  const indexes = new Map(nextClaims.map((item, index) => [item.claimId, index]));
  const pages = patch.pages.map(page => resolvePage(page, indexes));
  return { draft: { claims: nextClaims.map(item => item.claim), pages, summary: patch.summary }, stableClaims: nextClaims };
}

function validatePatchShape(patch: CorrectionPatch, current: readonly StableClaimEntry[], permissions: CorrectionPermissions): void {
  const known = new Set(current.map(item => item.claimId));
  const locked = new Set(permissions.lockedClaimIds);
  const mutable = new Set(current.filter(item => !locked.has(item.claimId)).map(item => item.claimId));
  validateClaimUpdates(patch.claimUpdates, known, mutable, permissions);
  validateDroppedClaimIds(patch.droppedClaimIds, known, mutable);
  if (patch.pages.length === 0) throw new Error("correction omitted planned pages");
}

function validateClaimUpdates(updates: readonly ClaimUpdate[], known: ReadonlySet<string>, mutable: ReadonlySet<string>,
  permissions: CorrectionPermissions): void {
  const seenUpdates = new Set<string>();
  for (const update of updates) {
    if (!known.has(update.claimId) || !mutable.has(update.claimId) || seenUpdates.has(update.claimId)) {
      throw new Error(`invalid or duplicate correction claimId: ${update.claimId}`);
    }
    if (!Array.isArray(update.changes) || update.changes.length === 0) throw new Error(`empty correction update: ${update.claimId}`);
    validateFieldChanges(update, permissions);
    seenUpdates.add(update.claimId);
  }
}

function validateDroppedClaimIds(claimIds: readonly string[], known: ReadonlySet<string>, mutable: ReadonlySet<string>): void {
  const seenDrops = new Set<string>();
  for (const claimId of claimIds) {
    if (!known.has(claimId) || !mutable.has(claimId) || seenDrops.has(claimId)) {
      throw new Error(`invalid or duplicate dropped claimId: ${claimId}`);
    }
    seenDrops.add(claimId);
  }
}

function resolveEntryUpdate(entry: StableClaimEntry, update: ClaimUpdate | undefined, permissions: CorrectionPermissions,
  catalog: readonly CorrectionEvidence[], frozenPages: readonly PlannedPage[]): StableClaimEntry {
  if (!update) return entry;
  const changes = updateFieldValues(update);
  const evidence = resolveUpdatedEvidence(entry, changes, permissions, catalog);
  const fields = omitPatchReferences(changes);
  const page = correctionTargetPage(fields.targetPageId ?? entry.claim.targetPageId, frozenPages);
  const claim = { ...entry.claim, ...fields, targetPageId: page.pageId, topic: page.topic, decisionObject: page.decisionObject,
    evidenceId: evidence.primary.evidenceId, quote: evidence.primary.quote, supportingQuotes: evidence.supportingQuotes };
  assertAuthorityCompatible(claim, evidence.primary.evidenceId, catalog, entry.claimId);
  return { claimId: entry.claimId, claim, quoteId: evidence.primaryQuoteId, supportingQuoteIds: evidence.supportingQuoteIds };
}

function resolveUpdatedEvidence(entry: StableClaimEntry, changes: Record<string, unknown>, permissions: CorrectionPermissions,
  catalog: readonly CorrectionEvidence[]) {
  const primaryQuoteId = (changes.quoteId as string | undefined) ?? entry.quoteId;
  const requestedSupports = (changes.supportingQuoteIds as string[] | undefined) ?? entry.supportingQuoteIds;
  const supportingQuoteIds = normalizedSupportQuoteIds(primaryQuoteId, requestedSupports);
  const changed = entry.quoteId !== primaryQuoteId
    || JSON.stringify(entry.supportingQuoteIds) !== JSON.stringify(supportingQuoteIds);
  if (changed && !permissions.replaceEvidenceForClaimIds.includes(entry.claimId)) {
    throw new Error(`evidence replacement was not authorized for ${entry.claimId}`);
  }
  const primary = resolveQuote(catalog, primaryQuoteId);
  assertNoAuthorityPromotion(entry, primary.evidenceId, catalog, changed);
  const supportingQuotes = supportingQuoteIds.map(quoteId => {
    const quote = resolveQuote(catalog, quoteId);
    return { evidenceId: quote.evidenceId, quote: quote.quote };
  });
  return { primaryQuoteId, primary, supportingQuotes, supportingQuoteIds };
}

function correctionTargetPage(targetPageId: string | null | undefined, pages: readonly PlannedPage[]): PlannedPage {
  const page = pages.find(item => item.pageId === targetPageId);
  if (!page) throw new Error(`unknown correction target page: ${targetPageId ?? "null"}`);
  return page;
}

function validateFieldChanges(update: ClaimUpdate, permissions: CorrectionPermissions): void {
  const seen = new Set<string>();
  for (const change of update.changes) {
    if (!change || !CLAIM_UPDATE_FIELDS.has(change.field) || !Object.prototype.hasOwnProperty.call(change, "value")
      || seen.has(change.field)) {
      throw new Error(`invalid or duplicate correction field for ${update.claimId}`);
    }
    if ((change.field === "quoteId" || change.field === "supportingQuoteIds")
      && !permissions.replaceEvidenceForClaimIds.includes(update.claimId)) {
      throw new Error(`evidence replacement was not authorized for ${update.claimId}`);
    }
    seen.add(change.field);
  }
}

function updateFieldValues(update: ClaimUpdate): Record<string, unknown> {
  return Object.fromEntries(update.changes.map(change => [change.field, change.value]));
}

function omitPatchReferences(fields: Record<string, unknown>): Partial<FlowClaim> {
  const { quoteId: _quoteId, supportingQuoteIds: _supportingQuoteIds, ...claimFields } = fields;
  return claimFields as Partial<FlowClaim>;
}

function assertNoAuthorityPromotion(entry: StableClaimEntry, nextEvidenceId: string, catalog: readonly CorrectionEvidence[], changed: boolean): void {
  if (!changed) return;
  const previousRole = sourceRole(entry, catalog);
  const nextRole = catalog.find(item => item.id === nextEvidenceId)?.kind;
  if (!nextRole || ROLE_AUTHORITY[nextRole] > ROLE_AUTHORITY[previousRole]) {
    throw new Error(`correction cannot promote source authority for ${entry.claimId}`);
  }
}

function assertAuthorityCompatible(claim: FlowClaim, evidenceId: string, catalog: readonly CorrectionEvidence[], claimId: string): void {
  const role = catalog.find(item => item.id === evidenceId)?.kind;
  const violation = authorityViolation(claim, role);
  if (violation) throw new Error(`correction cannot promote ${correctionAuthorityLabels[violation]} for ${claimId}`);
}

const correctionAuthorityLabels = { assistant: "assistant evidence", artifact: "artifact evidence", decided: "decision authority" } as const;

function resolvePage(page: CorrectionPatch["pages"][number], indexes: ReadonlyMap<string, number>): TopicDraft["pages"][number] {
  const claimIndexes: number[] = [];
  const seen = new Set<string>();
  for (const claimId of page.claimIds) {
    if (!CLAIM_ID.test(claimId) || seen.has(claimId) || !indexes.has(claimId)) {
      throw new Error(`unknown, duplicate or dropped correction claimId: ${claimId}`);
    }
    seen.add(claimId);
    claimIndexes.push(indexes.get(claimId)!);
  }
  const body = page.body.replace(/\{\{claim:([^}]+)\}\}/g, (marker, claimId: string) => {
    const index = indexes.get(claimId);
    if (!CLAIM_ID.test(claimId) || index === undefined || !seen.has(claimId)) {
      throw new Error(`unknown or unlisted correction citation: ${claimId}`);
    }
    return `{{claim:${index}}}`;
  });
  const citationRetirements = page.citationRetirements?.map(item => ({ ...item,
    replacement: resolveRetirementReplacement(item.replacement, indexes, seen) }));
  return { pageId: page.pageId, body, claimIndexes,
    ...(citationRetirements?.length ? { citationRetirements } : {}) };
}

function resolveRetirementReplacement(replacement: string, indexes: ReadonlyMap<string, number>, pageClaims: ReadonlySet<string>): string {
  const match = replacement.match(/^\{\{claim:([^}]+)\}\}$/);
  if (!match) return replacement;
  const claimId = match[1];
  const index = indexes.get(claimId);
  if (!CLAIM_ID.test(claimId) || index === undefined || !pageClaims.has(claimId)) {
    throw new Error(`unknown or unlisted correction retirement citation: ${claimId}`);
  }
  return `{{claim:${index}}}`;
}
