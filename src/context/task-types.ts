/** Query-specific, project-scoped evidence shared by native hooks and MCP. */
import type { ContextPrimary } from "./types.js";
import type { SectionTemporalStatus } from "./task-temporal.js";
import type { ReviewedClaim } from "./ledger.js";

export interface TaskContextOptions {
  root: string;
  prompt: string;
  projectId?: string;
  scope?: "project" | "semantic";
  allowedPageIds?: string[];
}

interface EvidenceContent {
  title: string;
  updatedAt: string | null;
  decisionObject: string | null;
  section: string;
  text: string;
  qualifications: string;
  /** Explicit page/heading label, not a verified effective date or live implementation state. */
  temporalStatus?: SectionTemporalStatus;
  sources: ContextPrimary["sourceWindows"];
  sourceProjectIds?: string[];
}

/** Existing page evidence keeps its fields; the optional origin preserves legacy consumers. */
export interface PageTaskEvidence extends EvidenceContent {
  origin?: "page";
  pageId: string;
  pageRevision: string;
}

/** Reviewed claims have immutable record references and quotes, never synthetic page identifiers. */
export interface LedgerTaskEvidence extends EvidenceContent {
  origin: "ledger";
  pageId?: never;
  pageRevision?: never;
  claimRef: string;
  recordId: string;
  recordRevision: string;
  claimKind: ReviewedClaim["kind"];
  claimStatus: ReviewedClaim["status"];
  quotes: ReviewedClaim["quotes"];
}

export type TaskEvidence = PageTaskEvidence | LedgerTaskEvidence;

export interface TaskContext {
  version: 1;
  projectId: string | null;
  status: "ok" | "no-hit" | "degraded" | "ambiguous-scope";
  evidence: TaskEvidence[];
  complete: boolean;
  followUpPageIds: string[];
  followUpClaimRefs?: string[];
  diagnostics: { scopedPages: number; scopedClaims?: number; matchedSections: number; warnings: string[] };
}
