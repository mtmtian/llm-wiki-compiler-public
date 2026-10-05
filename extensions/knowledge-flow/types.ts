/**
 * Types for the conservative, project-scoped knowledge-flow extension.
 *
 * The extension deliberately keeps session evidence in memory until a claim
 * passes extraction and an independent review. Only the supporting quote is
 * later written to a compiler source file.
 */

import type { LLMProvider } from "../../src/utils/provider.js";
import type { TopicMerge, TopicRevision, TopicMigration } from "./topic-revision-types.js";

/** Hard ceiling shared by extraction, review schemas, and publication guards. */
export const MAX_PROPOSALS = 5;

/** A source fragment supplied by a host hook or a task runner. */
export interface FlowEvidence {
  id: string;
  kind: "user" | "assistant" | "artifact";
  text: string;
  locator: string;
  sha256: string;
  observedAt: string;
  /** Hash of the complete sanitized original, retained when sharing only a quote. */
  originalSha256?: string;
  /** Prompt-only: from the turns consolidated now ("current") or earlier session context; never published. */
  origin?: "current" | "earlier";
}

/** One completed project task presented to the knowledge flow. */
export interface FlowJob {
  id: string;
  projectId: string;
  projectLabel: string;
  sessionId: string;
  turnId: string;
  cwd: string;
  createdAt: string;
  prompt: string;
  lastAssistant: string;
  evidence: FlowEvidence[];
  /** Frozen page organization contract; absent means legacy project ownership. */
  topicScope?: "semantic";
  /** Accepted concept page ids allowed by this job's frozen scope. */
  allowedPageIds: string[];
  /** Quotes received from the shared proposal inbox, revalidated before review. */
  submittedClaims?: FlowClaim[];
  /** Complete immutable record set observed before extraction/review begins. */
  basisRecordIds?: string[];
  /** Explicit new attempt for a held batch; preserves evidence without replaying its session cursor. */
  reviewRetryOf?: string;
  /** Durable local context is a navigation aid; only original evidence can support claims. */
  sessionContext?: { version: 1; revision: number; summary: string; topicPageIds: string[]; evidence: FlowEvidence[] };
}

/** Runtime configuration. Provider fields are intentionally injectable for tests. */
export interface FlowConfig {
  /** Enabled only after all replica participants attest semantic revision support. */
  topicScope?: "semantic";
  /** Set by the host only while the shared knowledge-ledger gate is enabled (ledger_gate.py). */
  knowledgeLedger?: boolean;
  wikiRoot: string;
  stateDir: string;
  model: "gpt-5.6-luna" | string;
  maxProposals: number;
  maxPendingPerProject: number;
  machineId?: string;
  /** Optional project-to-page ownership map used when baseline metadata lacks projectId. */
  projects?: Record<string, { pages?: string[]; label?: string }>;
  /** Reviewed historical routing snapshot supplied by replica sync. */
  topicRoutes?: TopicRouteGroup[];
  topicMigration?: TopicMigration;
  /** Reviewed merges of revision-layer pages, applied between earlier and later revisions (topic-merge.ts). */
  topicMerges?: TopicMerge[];
  sessionConsolidation?: { enabled?: boolean; quietSeconds?: number; maxWaitSeconds?: number };
  publishEnabled?: boolean;
  sharedWikiRoot?: string;
  exchange?: { root: string; protocolVersion?: number; publisherMachineId?: string; legacyImporterMachineId?: string; participants: string[] };
  provider?: LLMProvider;
  reviewer?: LLMProvider;
}

/** Exact legacy contributions reviewed together for one canonical destination. */
export interface TopicRouteGroup {
  projectId: string;
  topic: string;
  decisionObject: string;
  claimRefs: string[];
}

/** A bounded proposal returned by the extraction model. */
export interface FlowClaim {
  text: string;
  evidenceId: string;
  quote: string;
  title: string;
  topic: string;
  /** Stable decision object used with topic identity; absent on legacy publications. */
  decisionObject?: string;
  slug: string;
  targetPageId: string | null;
  kind: "decision" | "fact" | "constraint" | "lesson";
  status: "decided" | "historical" | "uncertain";
  useWhen: string;
  rationale: string;
  /** A model-requested full replacement is always sent to review. */
  replacementIntent?: boolean;
  /** Additional exact original quotes needed for a decision spanning several turns. */
  supportingQuotes?: Array<{ evidenceId: string; quote: string }>;
}

/** Review disposition for one claim. */
export interface FlowReviewDecision {
  index: number;
  decision: "accept" | "reject" | "needs_review";
  reason: string;
  /** Existing page ids whose evidence conflicts with this claim. */
  conflictingPageIds: string[];
}

/** A reviewer's conclusion on one claim; the page-level decision still decides the outcome. */
export interface ClaimDecision {
  claimIndex: number;
  decision: "accept" | "reject" | "needs_review";
  reason: string;
}

/**
 * One review attempt's per-claim conclusions, recorded for the ledger observation period
 * (deployment/KNOWLEDGE-LEDGER.md §7.2). `complete` is false unless every claim has exactly one conclusion.
 */
export interface ClaimReview {
  /** `pruned` reviews the final draft restricted to the claims the previous review accepted (claim-pruning.ts). */
  stage: "initial" | "correction" | "pruned";
  decision: ClaimDecision["decision"];
  complete: boolean;
  claims: ClaimDecision[];
}

/** Result returned to the host route. */
export interface FlowResult {
  status: "published" | "submitted" | "empty" | "needs_review" | "deferred" | "error";
  publishedPageIds: string[];
  reviewCount: number;
  candidateIds?: string[];
  reviewFile?: string;
  error?: string;
  /** False ends an invalid frozen attempt; omitted errors retain the host's bounded execution retry. */
  retryable?: boolean;
  submissionId?: string;
  contribution?: { claims: FlowClaim[]; evidence: FlowEvidence[]; topicRevisions?: TopicRevision[] };
  sessionMemory?: { summary: string; topicPageIds: string[] };
  /** Per-claim conclusions of every review attempt in this batch; observation only until the ledger gate. */
  claimReviews?: ClaimReview[];
  /** Accepted claims from an unfinished page; terminal failure or hold preserves their ledger publication. */
  ledgerContribution?: { claims: FlowClaim[]; evidence: FlowEvidence[] };
}

/** Internal dependency seam used by tests and by alternate local hosts. */
export interface FlowDependencies {
  extract(provider: LLMProvider, job: FlowJob, existing: ReadonlyMap<string, string>, max: number): Promise<FlowClaim[]>;
  review(provider: LLMProvider, job: FlowJob, claims: FlowClaim[], existing: ReadonlyMap<string, string>): Promise<FlowReviewDecision[]>;
}
