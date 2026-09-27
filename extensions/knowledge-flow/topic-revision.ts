/** Validation and ordering helpers for model-reviewed whole-page revisions. */
import { sha256Text } from "../../src/connectors/hash.js";
import { parseQualifiedPageId } from "../../src/utils/page-id.js";
import { parseFrontmatter } from "../../src/utils/markdown.js";
import { acceptedPublicationClaims } from "./publication-validation.js";
import type { FlowClaim } from "./types.js";
import type { PublicationRecord, PublicationConflict, PublicationEntry } from "./publication-types.js";
import type { TopicRevision } from "./topic-revision-types.js";
import { validateRetirementShape } from "./citation-retirement.js";

const HASH = /^[a-f0-9]{64}$/;
const PLACEHOLDER = /\{\{claim:(\d+)\}\}/g;

/** Stable identity independent of display title or filename. */
export function stableTopicId(projectId: string, topic: string, decisionObject: string): string {
  const normalize = (value: string) => value.normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase();
  return sha256Text(JSON.stringify([projectId, normalize(topic), normalize(decisionObject)]));
}

/** Claims referenced by revision bodies become source entries even without append segments. */
export function revisionEntries(records: PublicationRecord[]): PublicationEntry[] {
  return records.flatMap(record => (record.payload.topicRevisions ?? []).flatMap(revision =>
    revision.claimIndexes.map(index => ({ ref: `${record.id}:${index}`, record, claim: record.payload.claims[index], index }))));
}

/** Validate every claim/evidence and every revision body before touching a page. */
export function validateTopicRevisionRecord(record: PublicationRecord): TopicRevision[] {
  const revisions = record.payload.topicRevisions;
  if (!Array.isArray(revisions) || revisions.length === 0) throw new Error("topic revision list is required");
  const allowed = revisions.map(revision => revision.pageId);
  const claims = acceptedPublicationClaims(record, allowed);
  return validateRevisionSet(revisions, claims, allowed);
}

function validateRevisionSet(revisions: TopicRevision[], claims: FlowClaim[], allowed: string[]): TopicRevision[] {
  const seenPages = new Set<string>(); const covered = new Set<number>();
  const normalized = revisions.map(revision => validateRevision(revision, claims, allowed, seenPages, covered));
  if (covered.size !== claims.length) throw new Error("topic revision claims are omitted");
  return normalized;
}

function validateRevision(revision: TopicRevision, claims: FlowClaim[], allowed: string[], seenPages: Set<string>, covered: Set<number>): TopicRevision {
  requireRevisionShape(revision, allowed, seenPages, claims.length);
  seenPages.add(revision.pageId);
  const expected = validateRevisionPlaceholders(revision);
  validateRevisionCoverage(expected, revision.pageId, claims, covered);
  if (revision.basisHash !== null && !HASH.test(revision.basisHash)) throw new Error("topic revision basis hash is invalid");
  return { ...revision, claimIndexes: expected };
}

function requireRevisionShape(revision: TopicRevision, allowed: string[], seenPages: Set<string>, claimCount: number): void {
  requireRevisionPage(revision, allowed);
  if (seenPages.has(revision.pageId)) throw new Error("topic revision page appears more than once in a record");
  if (!HASH.test(revision.topicId)) throw new Error("topic revision topic id is invalid");
  if (revision.topicScope !== undefined && revision.topicScope !== "semantic") throw new Error("topic revision scope is invalid");
  requireRevisionMetadata(revision); requireRevisionBody(revision); requireRevisionIndexes(revision, claimCount);
  validateRetirementShape(revision.citationRetirements, revision.body, revision.claimIndexes);
}

function validateRevisionPlaceholders(revision: TopicRevision): number[] {
  const placeholders = [...revision.body.matchAll(PLACEHOLDER)].map(match => Number(match[1]));
  if (revision.body.replace(PLACEHOLDER, "").includes("{{claim:")) throw new Error("topic revision contains an invalid claim placeholder");
  const expected = [...new Set(revision.claimIndexes)].sort((a, b) => a - b);
  const actual = [...new Set(placeholders)].sort((a, b) => a - b);
  if (expected.length !== actual.length || expected.some((index, i) => index !== actual[i])) throw new Error("topic revision body does not cite every claim");
  return expected;
}

function validateRevisionCoverage(indexes: number[], pageId: string, claims: FlowClaim[], covered: Set<number>): void {
  for (const index of indexes) {
    if (covered.has(index)) throw new Error("topic revision claim is assigned twice");
    covered.add(index);
    if (claims[index].targetPageId !== pageId) throw new Error("topic revision claim target does not match page");
  }
}

function requireRevisionPage(revision: TopicRevision, allowed: string[]): void {
  if (!revision || typeof revision !== "object" || !isPageId(revision.pageId) || !allowed.includes(revision.pageId)) throw new Error("topic revision page path is invalid");
}
function requireRevisionMetadata(revision: TopicRevision): void {
  if (typeof revision.title !== "string" || !revision.title.trim() || typeof revision.topic !== "string" || !revision.topic.trim() || typeof revision.decisionObject !== "string") throw new Error("topic revision metadata is invalid");
}
function requireRevisionBody(revision: TopicRevision): void {
  if (typeof revision.body !== "string" || !revision.body.trim() || hasFrontmatter(revision.body)) throw new Error("topic revision body must be markdown without frontmatter");
}
function requireRevisionIndexes(revision: TopicRevision, claimCount: number): void {
  if (!Array.isArray(revision.claimIndexes) || revision.claimIndexes.length === 0 || revision.claimIndexes.some(index => !Number.isInteger(index) || index < 0 || index >= claimCount)) throw new Error("topic revision claim index is invalid");
}

function isPageId(value: unknown): value is string {
  const parsed = typeof value === "string" ? parseQualifiedPageId(value) : null;
  return Boolean(parsed && parsed.namespace === "concepts");
}

function hasFrontmatter(body: string): boolean { return /^---\r?\n/.test(body); }

/** A planned revision ready for deterministic rendering. */
export interface TopicRevisionApplication { record: PublicationRecord; revision: TopicRevision; previous: string | undefined; }

/** Validate ancestry, hold unobserved same-page variants, and return topological applications. */
export function planTopicRevisions(records: PublicationRecord[], existing: ReadonlyMap<string, string>): { applications: TopicRevisionApplication[]; conflicts: PublicationConflict[] } {
  const revisionRecords = records.filter(record => (record.payload.topicRevisions?.length ?? 0) > 0);
  const validated = new Map<string, TopicRevision[]>();
  for (const record of revisionRecords) validated.set(record.id, validateTopicRevisionRecord(record));
  const ordered = topological(revisionRecords); const held = new Set<string>(); const applications: TopicRevisionApplication[] = []; const conflicts: PublicationConflict[] = [];
  const working = new Map(existing); const concurrent = concurrentRevisionRecords(revisionRecords, validated); const applied = new Set<string>();
  const semanticPages = new Set([...existing].filter(([, body]) => parseFrontmatter(body).meta.topicScope === "semantic").map(([id]) => id));
  for (const record of ordered) {
    const revisions = validated.get(record.id) ?? [];
    const local = revisions.map(revision => ({ record, revision, previous: working.get(revision.pageId) }));
    if (local.every(item => isRevisionAlreadyPresent(item.previous, item.record, item.revision))) { applied.add(record.id); continue; }
    if (revisions.some(revision => revision.topicScope !== "semantic" && semanticPages.has(revision.pageId))) {
      holdRecord(record, revisions, conflicts, "legacy topic revision cannot update a semantic page"); held.add(record.id); continue;
    }
    if (concurrent.has(record.id)) { holdRecord(record, revisions, conflicts, "concurrent-topic-revisions: independent whole-page updates require review"); held.add(record.id); continue; }
    if (record.payload.basisRecordIds.some(id => held.has(id))) { holdRecord(record, revisions, conflicts, "revision depends on a held record"); held.add(record.id); continue; }
    for (const item of local) {
      applications.push(item); rememberRevision(item.revision, working, semanticPages);
    }
    applied.add(record.id);
  }
  return { applications, conflicts: conflicts.sort((a, b) => a.claimRefs.join().localeCompare(b.claimRefs.join())) };
}

/** Carry scope across the planned chain even though unrendered bodies have no frontmatter. */
function rememberRevision(revision: TopicRevision, working: Map<string, string>, semanticPages: Set<string>): void {
  working.set(revision.pageId, revision.body);
  if (revision.topicScope === "semantic") semanticPages.add(revision.pageId);
}

export function revisionBasisMatches(basis: string | null, previous: string | undefined): boolean { return previous === undefined ? basis === null : basis !== null && sha256Text(previous) === basis; }
function isRevisionAlreadyPresent(previous: string | undefined, record: PublicationRecord, revision: TopicRevision): boolean {
  if (!previous) return false;
  const metadata = parseFrontmatter(previous).meta;
  const refs = metadata.knowledgePublicationRefs;
  const present = Array.isArray(refs) && revision.claimIndexes.every(index => refs.includes(`${record.id}:${index}`));
  return present && (metadata.topicScope === "semantic" || revision.basisHash === null);
}
function holdRecord(record: PublicationRecord, revisions: TopicRevision[], conflicts: PublicationConflict[], reason: string): void {
  conflicts.push({ claimRefs: revisions.flatMap(revision => revision.claimIndexes.map(index => `${record.id}:${index}`)).sort(), recordIds: [record.id], reason });
}
function concurrentRevisionRecords(records: PublicationRecord[], validated: ReadonlyMap<string, TopicRevision[]>): Set<string> {
  const byPage = new Map<string, PublicationRecord[]>();
  for (const record of records) addRevisionRecords(byPage, record, validated.get(record.id) ?? []);
  const concurrent = new Set<string>();
  for (const group of byPage.values()) markConcurrent(group, concurrent);
  return concurrent;
}
function addRevisionRecords(byPage: Map<string, PublicationRecord[]>, record: PublicationRecord, revisions: TopicRevision[]): void {
  for (const revision of revisions) byPage.set(revision.pageId, [...(byPage.get(revision.pageId) ?? []), record]);
}
function markConcurrent(group: PublicationRecord[], concurrent: Set<string>): void {
  for (let leftIndex = 0; leftIndex < group.length; leftIndex += 1) for (let rightIndex = leftIndex + 1; rightIndex < group.length; rightIndex += 1) {
    const left = group[leftIndex]; const right = group[rightIndex];
    if (!left.payload.basisRecordIds.includes(right.id) && !right.payload.basisRecordIds.includes(left.id)) { concurrent.add(left.id); concurrent.add(right.id); }
  }
}
function topological(records: PublicationRecord[]): PublicationRecord[] {
  const pending = new Map(records.map(record => [record.id, record])); const result: PublicationRecord[] = [];
  while (pending.size) {
    const ready = [...pending.values()].filter(record => !record.payload.basisRecordIds.some(id => pending.has(id))).sort((a, b) => a.payload.createdAt.localeCompare(b.payload.createdAt) || a.id.localeCompare(b.id));
    if (!ready.length) throw new Error("cyclic topic revision basis");
    for (const record of ready) { result.push(record); pending.delete(record.id); }
  }
  return result;
}
