/** Bounded structured outputs for planning topics, editing whole pages and independent review. */
import { CLAIM_SCHEMA } from "./extract.js";
import type { LLMTool } from "../../src/utils/provider.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";
import { MAX_TOPIC_BODY_CHARS } from "./consolidation-plan.js";

const text = (maxLength: number) => ({ type: "string", minLength: 1, maxLength });
const object = (properties: Record<string, unknown>, optional: Record<string, unknown> = {}) => ({
  type: "object", additionalProperties: false, properties: { ...properties, ...optional }, required: Object.keys(properties) });
const array = (items: unknown, maxItems: number) => ({ type: "array", items, maxItems });
const index = { type: "integer", minimum: 0, maximum: 4 };
/** Correction-only replacement contract: one literal survivor, never prose or a list. */
const retirementReplacement = { anyOf: [
  { type: "string", minLength: 1, maxLength: 2048, pattern: "^\\^\\[[^\\]\\r\\n]+\\]$" },
  { type: "string", minLength: 1, maxLength: 32, pattern: "^\\{\\{claim:[0-4]\\}\\}$" },
  { type: "string", minLength: 1, maxLength: 2048, pattern: "^https://[^\\s]+$" },
] };
const retirement = object({ citation: text(1024), reason: text(1000),
  // The first draft still enters the existing deterministic correction path;
  // correction retries use the literal-only schema below.
  replacement: text(2048) });
const pageEdit = object({ pageId: text(180), body: text(MAX_TOPIC_BODY_CHARS), claimIndexes: array(index, 5) },
  { citationRetirements: array(retirement, 500) });
const verdict = { enum: ["accept", "reject", "needs_review"] };
/**
 * Per-claim conclusions are optional here so a missing or miscounted list can never fail a review:
 * until the ledger gate is enabled they are only recorded (claim-decisions.ts). Codex's strict wire
 * schema still requires the property, so the reviewer always returns it.
 */
const MAX_CLAIM_DECISIONS = 20;
const claimDecisions = (claimIndex: Record<string, unknown>, maxItems = MAX_CLAIM_DECISIONS) =>
  array(object({ claimIndex, decision: verdict, reason: text(2000) }), maxItems);
const reviewOutput = object({ decision: verdict, reason: text(2000),
  checkedClaimIndexes: array(index, 5), checkedPageIds: array(text(180), 5) },
  { checkedRetiredCitations: array(text(1024), 500), claimDecisions: claimDecisions(index),
    quoteRepairs: array(object({ claimIndex: index, quoteId: text(180) }), 5) });

function allowedStrings(values: readonly string[], maxLength: number): Record<string, unknown> {
  return values.length ? { ...text(maxLength), enum: [...new Set(values)] } : { not: {} };
}

function allowedIndexes(values: readonly number[]): Record<string, unknown> {
  return values.length ? { type: "integer", enum: [...new Set(values)] } : { type: "integer" };
}

function nullableAllowed(values: readonly string[], maxLength: number): Record<string, unknown> {
  return values.length ? { anyOf: [allowedStrings(values, maxLength), { type: "null" }] } : { type: "null" };
}

type ObjectSchema = { properties: Record<string, unknown>; required?: string[] };
type ArrayObjectSchema = { items: ObjectSchema };
type CorrectionRole = "user" | "assistant" | "artifact";

const CORRECTION_KINDS: Record<CorrectionRole, readonly string[]> = {
  user: ["decision", "fact", "constraint", "lesson"],
  assistant: ["lesson"],
  artifact: ["fact", "lesson", "constraint"],
};

/** Plan destinations before extracting individual claims. */
export const planTool: LLMTool = { name: "knowledge_topic_plan", description: "Reuse the existing workstream page, or justify a new workstream.",
  input_schema: object({ summary: text(4000), disposition: { enum: ["edit", "noop", "needs_review"] }, reason: text(1000),
    pages: array(object({ action: { enum: ["update", "create"] },
      targetPageId: { anyOf: [text(180), { type: "null" }] }, title: text(160), topic: text(160),
      decisionObject: text(80), reason: text(1000) }), 5) }) };

/** Bind corrected destinations and keep non-edit plans structurally empty. */
export function createPlanTool(allowedPageIds: readonly string[]): LLMTool {
  const tool = structuredClone(planTool);
  const properties = tool.input_schema.properties as Record<string, ArrayObjectSchema>;
  const pages = properties.pages;
  pages.items.properties.targetPageId = nullableAllowed(allowedPageIds, 180);
  // A nested union is compatible with strict model schemas and allows the
  // correction to reconsider edit/noop/hold without returning contradictory data.
  tool.input_schema = object({ plan: { anyOf: [
    object({ summary: text(4000), disposition: { enum: ["edit"] }, reason: text(1000),
      pages: { ...pages, minItems: 1 } }),
    object({ summary: text(4000), disposition: { enum: ["noop", "needs_review"] }, reason: text(1000),
      pages: { ...pages, maxItems: 0 } }),
  ] } });
  return tool;
}

/** Claims retain their established evidence schema; prose is edited once per destination. */
export const editTool: LLMTool = { name: "knowledge_topic_edit", description: "Write coherent topic revisions and evidence-bound claims.",
  input_schema: object({ claims: (CLAIM_SCHEMA.properties as Record<string, unknown>).claims,
    pages: array(pageEdit, 5), summary: text(4000) }) };

/**
 * Bind both evidence references and page destinations to the frozen request.
 * The runtime validator remains authoritative; this schema only prevents
 * common hallucinated IDs before a correction attempt consumes a model call.
 */
export function createEditTool(destinationPageIds: readonly string[], evidenceIds: readonly string[]): LLMTool {
  const tool = structuredClone(editTool);
  const properties = tool.input_schema.properties as Record<string, ArrayObjectSchema>;
  const claimItem = properties.claims.items;
  claimItem.properties.evidenceId = allowedStrings(evidenceIds, 120);
  claimItem.properties.targetPageId = nullableAllowed(destinationPageIds, 180);
  properties.pages.items.properties.pageId = allowedStrings(destinationPageIds, 180);
  return tool;
}

/** Correction schema: select frozen quote IDs; never rewrite source text. */
export function createCorrectionEditTool(
  destinationPageIds: readonly string[], catalog: readonly CorrectionEvidence[],
): LLMTool {
  const tool = createEditTool(destinationPageIds, catalogEvidenceIds(catalog));
  const properties = tool.input_schema.properties as Record<string, unknown>;
  const claims = properties.claims as ArrayObjectSchema;
  const baseClaim = claims.items;
  delete baseClaim.properties.quote;
  delete baseClaim.properties.evidenceId;
  delete baseClaim.properties.topic;
  delete baseClaim.properties.decisionObject;
  delete baseClaim.properties.targetPageId;
  baseClaim.required = [...(baseClaim.required ?? []).filter(name => !["quote", "evidenceId", "topic", "decisionObject", "targetPageId"].includes(name)), "quoteId", "targetPageId"];
  const branches = (Object.keys(CORRECTION_KINDS) as CorrectionRole[])
    .map(role => correctionClaimBranch(baseClaim, role, catalog, destinationPageIds))
    .filter((branch): branch is Record<string, unknown> => branch !== null);
  properties.claims = branches.length
    ? { ...claims, items: { anyOf: branches } }
    : { ...claims, maxItems: 0, items: emptyObjectSchema() };
  const pages = properties.pages as ArrayObjectSchema;
  (pages.items.properties.citationRetirements as ArrayObjectSchema).items.properties.replacement = retirementReplacement;
  return tool;
}

function correctionClaimBranch(baseClaim: ObjectSchema, role: CorrectionRole,
  catalog: readonly CorrectionEvidence[], destinationPageIds: readonly string[]): Record<string, unknown> | null {
  const primary = roleEvidence(catalog, role);
  if (!primary.length) return null;
  const support = role === "user" ? catalog.filter(item => item.quoteOptions.length > 0)
    : catalog.filter(item => item.kind !== "assistant" && item.quoteOptions.length > 0);
  const properties = { ...baseClaim.properties,
    quoteId: allowedStrings(primary.flatMap(item => item.quoteOptions.map(option => option.quoteId)), 180),
    targetPageId: allowedStrings(destinationPageIds, 180),
    kind: { enum: [...CORRECTION_KINDS[role]] }, status: { enum: role === "user" ? ["decided", "historical"] : ["historical"] },
    supportingQuotes: correctionSupportSchema(support),
  };
  return { type: "object", additionalProperties: false, properties, required: [...(baseClaim.required ?? [])] };
}

function correctionSupportSchema(support: readonly CorrectionEvidence[]): Record<string, unknown> {
  if (!support.length) return { type: "array", maxItems: 0, items: emptyObjectSchema() };
  return array(object({ quoteId: allowedStrings(support.flatMap(item => item.quoteOptions.map(option => option.quoteId)), 180) }), 3);
}

function emptyObjectSchema(): Record<string, unknown> {
  return { type: "object", additionalProperties: false, properties: {}, required: [] };
}

function roleEvidence(catalog: readonly CorrectionEvidence[], role: CorrectionRole): CorrectionEvidence[] {
  return catalog.filter(item => item.kind === role && item.quoteOptions.length > 0);
}

function catalogEvidenceIds(catalog: readonly CorrectionEvidence[]): string[] {
  return [...new Set(catalog.filter(item => item.quoteOptions.length > 0).map(item => item.id))];
}

/** Acceptance covers every claim and every complete page diff, including removed text. */
export const topicReviewTool: LLMTool = { name: "knowledge_topic_review", description: "Independently verify evidence, routing, history and the entire edit.",
  input_schema: reviewOutput };

/**
 * Bind review arrays to the actual claims, revised pages and retirements in this run.
 * Catalog pages remain context only and cannot be claimed as reviewed output.
 */
export function createTopicReviewTool(claimCount: number, pageIds: readonly string[],
  retirementCitations: readonly string[], quoteIds: readonly string[] = []): LLMTool {
  const claimIndexes = Array.from({ length: claimCount }, (_, value) => value);
  // Empty scopes are enforced by maxItems: 0; Codex does not support `not`.
  const pageSchema = pageIds.length ? allowedStrings(pageIds, 180) : text(180);
  const retirementSchema = retirementCitations.length ? allowedStrings(retirementCitations, 1024) : text(1024);
  const quoteIdSchema = quoteIds.length ? allowedStrings(quoteIds, 180) : text(180);
  const quoteRepairSchema = array(object({ claimIndex: index, quoteId: quoteIdSchema }),
    quoteIds.length ? Math.min(5, claimCount) : 0);
  return { name: topicReviewTool.name, description: topicReviewTool.description,
    input_schema: object({ decision: verdict, reason: text(2000),
      checkedClaimIndexes: array(allowedIndexes(claimIndexes), Math.min(5, claimIndexes.length)),
      checkedPageIds: array(pageSchema, Math.min(5, pageIds.length)) },
    { checkedRetiredCitations: array(retirementSchema, Math.min(500, retirementCitations.length)),
      claimDecisions: claimDecisions(allowedIndexes(claimIndexes), claimCount ? MAX_CLAIM_DECISIONS : 0),
      quoteRepairs: quoteRepairSchema }) };
}
