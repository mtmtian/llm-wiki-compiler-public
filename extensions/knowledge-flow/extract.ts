/**
 * Evidence-bound claim extraction for knowledge-flow.
 *
 * Extraction is intentionally narrower than `compile`: it asks for at most a
 * few reusable claims and rejects every quote that cannot be found verbatim in
 * the host-supplied evidence.
 */
import { DURABLE_KNOWLEDGE_POLICY } from "../../src/compiler/knowledge-policy.js";

import { slugify } from "../../src/utils/markdown.js";
import { isSafeFilenameComponent } from "../../src/profile/identity.js";
import { sha256Text } from "../../src/connectors/hash.js";
import type { LLMMessage, LLMProvider, LLMTool } from "../../src/utils/provider.js";
import { MAX_PROPOSALS } from "./types.js";
import type { FlowClaim, FlowEvidence, FlowJob } from "./types.js";
import { EVIDENCE_SUPPORT_RULES } from "./consolidation-prompts.js";

export const CLAIM_SCHEMA: Record<string, unknown> = {
  type: "object",
  additionalProperties: false,
  properties: {
    claims: {
      type: "array",
      maxItems: MAX_PROPOSALS,
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          text: { type: "string", minLength: 1, maxLength: 1200 },
          evidenceId: { type: "string", minLength: 1, maxLength: 120 },
          quote: { type: "string", minLength: 1, maxLength: 600 },
          title: { type: "string", minLength: 1, maxLength: 160 },
          topic: { type: "string", minLength: 1, maxLength: 160 },
          decisionObject: { type: "string", minLength: 1, maxLength: 160 },
          slug: { type: "string", minLength: 1, maxLength: 100 },
          targetPageId: { anyOf: [{ type: "string", maxLength: 180 }, { type: "null" }] },
          kind: { enum: ["decision", "fact", "constraint", "lesson"] },
          status: { enum: ["decided", "historical", "uncertain"] },
          useWhen: { type: "string", minLength: 1, maxLength: 500 },
          rationale: { type: "string", minLength: 1, maxLength: 500 },
          replacementIntent: { type: "boolean" },
          supportingQuotes: { type: "array", maxItems: 3, items: { type: "object", additionalProperties: false,
            properties: { evidenceId: { type: "string", minLength: 1, maxLength: 120 },
              quote: { type: "string", minLength: 1, maxLength: 600 } }, required: ["evidenceId", "quote"] } },
        },
        required: ["text", "evidenceId", "quote", "title", "topic", "decisionObject", "slug", "targetPageId", "kind", "status", "useWhen", "rationale", "replacementIntent", "supportingQuotes"],
      },
    },
  },
  required: ["claims"],
};

/** Machine-readable reasons that a model proposal cannot enter a draft. */
export interface ClaimDiagnostic {
  index: number;
  code: "proposal_limit" | "invalid_shape" | "unknown_evidence" | "unsafe_evidence"
    | "quote_mismatch" | "assistant_authority" | "invalid_supporting_quote"
    | "assistant_support_authority" | "out_of_scope_page" | "invalid_slug" | "invalid_kind" | "invalid_status"
    | "uncertain" | "duplicate";
  message: string;
  evidenceId?: string;
  targetPageId?: string | null;
}

/** A validated subset plus precise reasons for every rejected proposal. */
export interface ClaimDiagnostics {
  claims: FlowClaim[];
  diagnostics: ClaimDiagnostic[];
}

/** Extract a bounded set of claims from one job. */
export async function extractClaims(
  provider: LLMProvider,
  job: FlowJob,
  existing: ReadonlyMap<string, string>,
  maxProposals: number,
): Promise<FlowClaim[]> {
  const prompt = extractionPrompt(job, existing, maxProposals);
  const raw = await provider.toolCall(extractionSystem(), [{ role: "user", content: prompt }], [claimTool()], 4000);
  return validateClaims(parseClaims(raw), job.evidence, job.allowedPageIds, maxProposals);
}

/** Expose the schema for provider adapters and contract tests. */
function claimTool(): LLMTool {
  return { name: "knowledge_claims", description: "Return only durable evidence-backed claims.", input_schema: CLAIM_SCHEMA };
}

function extractionSystem(): string {
  return DURABLE_KNOWLEDGE_POLICY + EVIDENCE_SUPPORT_RULES + "\n\nExtract only durable project knowledge with future practical value. Return zero claims when nothing changes future decisions. " +
    "Do not summarize a whole conversation. User requests, plans, or assistant claims are not proof of implementation or effectiveness. " +
    "A user decision may be decided; a report or artifact may only be historical and cannot prove deployment, tests, or production success. " +
    "A captured assistant message may support only a durable historical lesson or attributed analysis/report under the shared evidence contract, never a decided rule, verified metric, test result, or implementation fact. " +
    "Do not preserve a completion claim as a lesson. Keep uncertain analysis uncertain. " +
    "Before creating a page, first match an existing page in the same project using the canonical topic and decisionObject; when matched, reuse its targetPageId. " +
    "Existing page metadata knowledgeTopic and knowledgeDecisionObject are valid matching signals and may be reused. " +
    "One publication may contain multiple complementary claims sharing one target page; claims for the same topic and decision object must keep the same title/slug. " +
    "Create a new page only when no matching page exists. Preserve evidence-bound rationale, useWhen, and tradeoffs; never turn unsupported context into a whole summary. " +
    "Never use the uncited assistant tail as evidence. Use the exact evidence id and an exact quote. Existing pages are context only, never evidence.";
}

function extractionPrompt(job: FlowJob, existing: ReadonlyMap<string, string>, max: number): string {
  const evidence = job.evidence.map(formatEvidence).join("\n\n");
  const pages = [...existing].map(([id, body]) => `PAGE ${id}:\n${body.slice(0, 12000)}`).join("\n\n");
  return `Project: ${job.projectId} (${job.projectLabel})\nTask: ${job.prompt}\nAssistant tail: ${job.lastAssistant.slice(-6000)}\n` +
    `Maximum claims: ${Math.min(max, MAX_PROPOSALS)}\nEvidence:\n${evidence || "(none)"}\nExisting project pages:\n${pages || "(none)"}`;
}

function formatEvidence(item: FlowEvidence): string {
  return `[${item.id}] kind=${item.kind} observedAt=${item.observedAt} locator=${item.locator}\n${item.text}`;
}

function parseClaims(raw: string): unknown[] {
  const parsed: unknown = JSON.parse(raw);
  if (!isRecord(parsed) || !Array.isArray(parsed.claims)) throw new Error("knowledge extraction returned an invalid claims object");
  return parsed.claims;
}

/** Reapply the same evidence and schema bounds to transported proposals. */
export function validateClaims(
  values: unknown[],
  evidence: FlowEvidence[],
  allowedPageIds: string[],
  max: number,
): FlowClaim[] {
  const result = diagnoseClaims(values, evidence, allowedPageIds, max);
  if (result.diagnostics.some(item => item.code === "proposal_limit")) {
    throw new Error("knowledge extraction exceeded proposal limit");
  }
  return result.claims;
}

/**
 * Explain rejected proposals without weakening the publication validator.
 * Correction prompts use these diagnostics to repair the same evidence rather
 * than inventing a replacement quote or silently dropping a decision.
 */
export function diagnoseClaims(
  values: unknown[], evidence: FlowEvidence[], allowedPageIds: string[], max: number,
): ClaimDiagnostics {
  const limit = Math.min(max, MAX_PROPOSALS);
  if (values.length > limit) return {
    claims: [], diagnostics: [{ index: -1, code: "proposal_limit", message: `at most ${limit} claims are allowed` }],
  };
  const byId = new Map(evidence.map((item) => [item.id, item]));
  const allowed = new Set(allowedPageIds);
  const claims: FlowClaim[] = [];
  const diagnostics: ClaimDiagnostic[] = [];
  values.forEach((value, index) => {
    const inspected = inspectClaim(value, index, byId, allowed);
    if (inspected.diagnostic) diagnostics.push(inspected.diagnostic);
    const claim = inspected.claim;
    if (claim && claims.some(item => item.text === claim.text)) {
      diagnostics.push({ index, code: "duplicate", message: "claim duplicates another proposal" });
    } else if (claim) {
      claims.push(claim);
    }
  });
  return { claims, diagnostics };
}

/** Keep correction feedback bounded and safe to place in a model prompt. */
export function formatClaimDiagnostics(diagnostics: ClaimDiagnostic[]): string {
  return diagnostics.slice(0, MAX_PROPOSALS + 1).map(item => {
    const evidence = item.evidenceId ? ` evidenceId=${item.evidenceId}` : "";
    const target = item.targetPageId ? ` targetPageId=${item.targetPageId}` : "";
    return `claim[${item.index}] ${item.code}:${item.message}${evidence}${target}`;
  }).join("; ");
}

function inspectClaim(
  value: unknown, index: number, byId: ReadonlyMap<string, FlowEvidence>, allowed: ReadonlySet<string>,
): { claim: FlowClaim | null; diagnostic?: ClaimDiagnostic } {
  if (!isBoundedClaim(value)) return rejected(index, "invalid_shape", "claim fields are missing or exceed bounds");
  const evidenceId = stringValue(value.evidenceId); const evidence = byId.get(evidenceId);
  if (!evidence) return rejected(index, "unknown_evidence", "evidenceId is not present in the frozen evidence", evidenceId);
  if (!isSafeEvidence(evidence)) return rejected(index, "unsafe_evidence", "evidence hash or content is invalid", evidenceId);
  const quote = stringValue(value.quote);
  if (!isExactQuote(evidence.text, quote)) return rejected(index, "quote_mismatch", "quote must be an exact source substring", evidenceId);
  if (evidence.kind === "assistant" && value.kind !== "lesson") {
    return rejected(index, "assistant_authority", "assistant primary evidence supports only historical lessons", evidenceId);
  }
  return inspectSupportingEvidence(value, index, byId, allowed);
}

/** Supporting assistant context can explain a user's choice, but cannot become its own authority. */
function inspectSupportingEvidence(value: Record<string, unknown>, index: number,
  byId: ReadonlyMap<string, FlowEvidence>, allowed: ReadonlySet<string>): ReturnType<typeof inspectClaim> {
  const evidenceId = stringValue(value.evidenceId);
  const evidence = byId.get(evidenceId)!;
  const supportingQuotes = additionalSupport(value.supportingQuotes, byId);
  if (supportingQuotes === null) {
    return rejected(index, "invalid_supporting_quote", "supporting quotes must cite exact frozen evidence", evidenceId);
  }
  if (supportingQuotes.some(support => byId.get(support.evidenceId)?.kind === "assistant") && evidence.kind !== "user") {
    return rejected(index, "assistant_support_authority", "assistant supporting evidence requires a user-primary claim", evidenceId);
  }
  return inspectDestination(value, index, byId, allowed);
}

/** Source authority is checked first; this stage diagnoses the destination and claim metadata. */
function inspectDestination(value: Record<string, unknown>, index: number,
  byId: ReadonlyMap<string, FlowEvidence>, allowed: ReadonlySet<string>): ReturnType<typeof inspectClaim> {
  const evidenceId = stringValue(value.evidenceId);
  const target = readTarget(value, allowed);
  if (!target.valid) return rejected(index, "out_of_scope_page", "targetPageId is not an allowed project page", evidenceId, target.value);
  if (!readSlug(value)) return rejected(index, "invalid_slug", "slug is not a safe topic slug", evidenceId, target.value);
  if (!isClaimKind(value.kind)) return rejected(index, "invalid_kind", "kind is not supported", evidenceId, target.value);
  const claim = normalizeClaim(value, byId, allowed);
  if (!claim) return rejected(index, "invalid_status", "claim status failed evidence validation", evidenceId, target.value);
  if (claim.status === "uncertain") return { claim, diagnostic: {
    index, code: "uncertain", message: "uncertain claims remain held for independent review", evidenceId, targetPageId: target.value,
  } };
  return { claim };
}

function rejected(
  index: number, code: ClaimDiagnostic["code"], message: string, evidenceId?: string, targetPageId?: string | null,
): { claim: null; diagnostic: ClaimDiagnostic } {
  return { claim: null, diagnostic: { index, code, message, ...(evidenceId ? { evidenceId } : {}), ...(targetPageId !== undefined ? { targetPageId } : {}) } };
}

function normalizeClaim(value: unknown, byId: ReadonlyMap<string, FlowEvidence>, allowed: ReadonlySet<string>): FlowClaim | null {
  if (!isBoundedClaim(value)) return null;
  const support = findSupport(value, byId);
  if (!support) return null;
  const supportingQuotes = additionalSupport(value.supportingQuotes, byId);
  if (supportingQuotes === null) return null;
  const target = readTarget(value, allowed);
  if (!target.valid) return null;
  const slug = readSlug(value);
  if (!slug) return null;
  const status = evidenceStatus(support.evidence, value.status);
  if (!isClaimKind(value.kind) || !isClaimStatus(status)) return null;
  const normalized: FlowClaim = {
    text: stringValue(value.text), evidenceId: support.evidence.id, quote: support.quote, title: stringValue(value.title),
    topic: stringValue(value.topic), slug, targetPageId: target.value, kind: value.kind, status,
    useWhen: stringValue(value.useWhen), rationale: stringValue(value.rationale), replacementIntent: value.replacementIntent === true,
    ...optionalDecisionObject(value),
    ...(supportingQuotes.length ? { supportingQuotes } : {}),
  };
  return normalized;
}

function isBoundedClaim(value: unknown): value is Record<string, any> {
  return isRecord(value) && boundedClaim(value);
}

/** Multi-turn approvals must cite original context as well as the approving message. */
function additionalSupport(value: unknown, evidence: ReadonlyMap<string, FlowEvidence>): NonNullable<FlowClaim["supportingQuotes"]> | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 3) return null;
  const result: NonNullable<FlowClaim["supportingQuotes"]> = [];
  for (const item of value) {
    const quote = supportQuote(item, evidence);
    if (!quote || result.some(prior => prior.evidenceId === quote.evidenceId && prior.quote === quote.quote)) return null;
    result.push(quote);
  }
  return result;
}

function supportQuote(item: unknown, evidence: ReadonlyMap<string, FlowEvidence>): { evidenceId: string; quote: string } | null {
  if (!isRecord(item) || typeof item.evidenceId !== "string" || typeof item.quote !== "string") return null;
  const source = evidence.get(item.evidenceId);
  return source && isSafeEvidence(source) && isExactQuote(source.text, item.quote)
    ? { evidenceId: item.evidenceId, quote: item.quote } : null;
}

/** Captured analysis stays dated context and cannot manufacture a user decision. */
function evidenceStatus(evidence: FlowEvidence, requested: unknown): string {
  const status = stringValue(requested);
  if (evidence.kind === "assistant") return status === "uncertain" ? status : "historical";
  return evidence.kind === "artifact" ? "historical" : status;
}

/** Provider schemas alone cannot validate files supplied through a shared inbox. */
function boundedClaim(value: Record<string, any>): boolean {
  const limits: Record<string, number> = { text: 1200, title: 160, topic: 160, slug: 100, useWhen: 500, rationale: 500 };
  return Object.entries(limits).every(([key, max]) => typeof value[key] === "string" &&
    value[key].trim().length > 0 && value[key].length <= max) &&
    (value.replacementIntent === undefined || typeof value.replacementIntent === "boolean") && validDecisionObject(value);
}

function validDecisionObject(value: Record<string, any>): boolean {
  if (!Object.prototype.hasOwnProperty.call(value, "decisionObject")) return true;
  const decisionObject = value.decisionObject;
  return typeof decisionObject === "string" && decisionObject.trim().length > 0 && decisionObject.length <= 160;
}

function optionalDecisionObject(value: Record<string, any>): Pick<FlowClaim, "decisionObject"> | Record<string, never> {
  return Object.prototype.hasOwnProperty.call(value, "decisionObject")
    ? { decisionObject: value.decisionObject.trim() }
    : {};
}

function findSupport(value: Record<string, any>, byId: ReadonlyMap<string, FlowEvidence>): { evidence: FlowEvidence; quote: string } | null {
  const evidence = byId.get(stringValue(value.evidenceId));
  const quote = stringValue(value.quote);
  if (evidence?.kind === "assistant" && value.kind !== "lesson") return null;
  return evidence && isSafeEvidence(evidence) && isExactQuote(evidence.text, quote) ? { evidence, quote } : null;
}

function readTarget(value: Record<string, any>, allowed: ReadonlySet<string>): { valid: boolean; value: string | null } {
  if (value.targetPageId === null) return { valid: true, value: null };
  const target = stringValue(value.targetPageId);
  return { valid: allowed.has(target) && isValidPageId(target), value: target };
}

function readSlug(value: Record<string, any>): string | null {
  const slug = slugify(stringValue(value.slug) || stringValue(value.title));
  return slug && /^[\p{L}\p{N}][\p{L}\p{N}-]*$/u.test(slug) ? slug : null;
}

function isValidPageId(id: string): boolean {
  const [namespace, slug, ...extra] = id.split("/");
  return namespace === "concepts" && extra.length === 0 && typeof slug === "string" && isSafeFilenameComponent(slug);
}

function isExactQuote(text: string, quote: string): boolean {
  return quote.length > 0 && quote.length <= 600 && !quote.includes("\r") && text.includes(quote);
}

function isSafeEvidence(item: FlowEvidence): boolean {
  return (item.kind === "user" || item.kind === "artifact" || item.kind === "assistant") &&
    /^[A-Za-z0-9._:-]+$/.test(item.id) && item.text.length > 0 && item.text.length <= 200_000 &&
    /^[a-f0-9]{64}$/.test(item.sha256) && sha256Text(item.text) === item.sha256;
}

function isClaimKind(value: unknown): value is FlowClaim["kind"] {
  return value === "decision" || value === "fact" || value === "constraint" || value === "lesson";
}

function isClaimStatus(value: unknown): value is FlowClaim["status"] {
  return value === "decided" || value === "historical" || value === "uncertain";
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
