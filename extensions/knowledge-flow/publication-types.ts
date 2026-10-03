/** Immutable multi-machine publication envelopes; compiler state stays machine-local. */
import type { FlowClaim, FlowEvidence } from "./types.js";
import type { TopicRevision } from "./topic-revision-types.js";

export interface PublicationRecord {
  id: string;
  payload: {
    /** 2: page-backed publication; 3: claims-only ledger record (deployment/KNOWLEDGE-LEDGER.md §7.1). */
    version: 2 | 3;
    baselineId: string;
    machineId: string;
    projectId: string;
    projectLabel: string;
    createdAt: string;
    originJobHash: string;
    repoIdentity: string | null;
    basisRecordIds: string[];
    claims: FlowClaim[];
    evidence: FlowEvidence[];
    topicRevisions?: TopicRevision[];
    review: { status: "accepted"; model: string };
  };
}

const LEDGER_VERSION = 3;

/**
 * Only legacy publications become appended page paragraphs: revisions replace whole
 * pages, and ledger records never touch page text. Any other version still reaches
 * the strict claim validation and fails there.
 */
export function rendersAsFragment(record: PublicationRecord): boolean {
  return record.payload.version !== LEDGER_VERSION && !record.payload.topicRevisions?.length;
}

export interface PublicationConflict {
  claimRefs: string[];
  recordIds: string[];
  reason: string;
}

/** One immutable contribution; its reference survives regrouping into topic pages. */
export interface PublicationEntry {
  ref: string;
  record: PublicationRecord;
  claim: FlowClaim;
  index: number;
  /** Only exact members of one reviewed migration group may bypass concurrency holds. */
  routingGroup?: string;
}

/** A reviewed topic destination plus its baseline prose and ordered contributions. */
export interface TopicPage {
  id: string;
  projectId: string;
  topic: string;
  decisionObject: string;
  original?: string;
  entries: PublicationEntry[];
}
