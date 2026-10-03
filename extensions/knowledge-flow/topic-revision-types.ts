/** Contracts for deterministic reviewed topic-page replacement and migration. */
import type { CitationRetirement } from "./citation-retirement.js";
import type { TopicPageRetirement } from "./page-retirement.js";

/** A reviewed, complete page body generated from one immutable publication. */
export interface TopicRevision {
  pageId: string;
  topicId: string;
  title: string;
  topic: string;
  decisionObject: string;
  /** Explicitly permits this revision to update a shared semantic topic page. */
  topicScope?: "semantic";
  /** SHA-256 of the complete prior page bytes; null means the page is new. */
  basisHash: string | null;
  /** Markdown body without YAML frontmatter. New claim citations use {{claim:N}}. */
  body: string;
  /** Global indexes into the enclosing publication's claims array. */
  claimIndexes: number[];
  /** Explicit exceptions to evidence retention, independently reviewed with the whole edit. */
  citationRetirements?: CitationRetirement[];
}

export interface TopicMigrationPreviousPage {
  pageId: string;
  sha256: string;
}

/** One reviewed migration destination, applied only against the frozen legacy basis. */
export interface TopicMigrationPage {
  projectId: string;
  projectLabel: string;
  pageId: string;
  topicId: string;
  title: string;
  topic: string;
  decisionObject: string;
  /** Markdown body; existing citation markers must be retained. */
  body: string;
  previousPages: TopicMigrationPreviousPage[];
  /** Exact removed markers and their reviewed surviving evidence or external record. */
  citationRetirements?: CitationRetirement[];
}

/** Reviewed legacy projection replacement manifest. */
export interface TopicMigration {
  version: 1;
  basisRecordIds: string[];
  pages: TopicMigrationPage[];
  /** Reviewed removal of pure process pages after durable knowledge has been transferred. */
  retiredPages?: TopicPageRetirement[];
}

/**
 * One reviewed merge of revision-layer topic pages (topic-merge.ts). The replica replays the absorbed
 * revision records first, checks that each previous page has exactly the reviewed bytes, replaces the
 * target with the reviewed body and removes the other pages; later records then apply to the merged page.
 */
export interface TopicMerge {
  /** The surviving page; it must be one of `previousPages`, so its page and topic identity are kept. */
  pageId: string;
  title: string;
  topic: string;
  decisionObject: string;
  /** Markdown body without frontmatter; every citation of the previous pages must stay or be retired. */
  body: string;
  /** Complete page bytes as rebuilt from the absorbed records. */
  previousPages: TopicMigrationPreviousPage[];
  /** Every revision record that touched a previous page before the merge was reviewed. */
  absorbedRecordIds: string[];
  /** ISO time of the reviewed merge; it becomes the merged page's updatedAt. */
  mergedAt: string;
  /** Why the previous pages are one long-running decision object. */
  reason: string;
  citationRetirements?: CitationRetirement[];
}
