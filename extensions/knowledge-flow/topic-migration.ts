/** Fail-closed application of reviewed legacy topic-page migration manifests. */
import { parseFrontmatter, buildFrontmatter } from "../../src/utils/markdown.js";
import { parseQualifiedPageId } from "../../src/utils/page-id.js";
import { sha256Text } from "../../src/connectors/hash.js";
import type { FlowConfig } from "./types.js";
import type { PublicationRecord, PublicationConflict } from "./publication-types.js";
import { rendersAsFragment } from "./publication-types.js";
import type { TopicMigration, TopicMigrationPage } from "./topic-revision-types.js";
import { validateCitationChanges, validateRetirementShape } from "./citation-retirement.js";
import { retirePageProvenance } from "./retirement-projection.js";
import type { PublicationSource } from "./materialize-render.js";
import { applyPageRetirements, validatePageRetirements } from "./page-retirement.js";
import type { CitationRetirement } from "./citation-retirement.js";
import type { RetirementReceipt } from "./retirement-receipt.js";

const HASH = /^[a-f0-9]{64}$/;

export interface TopicMigrationResult { pages: Map<string, string>; conflicts: PublicationConflict[]; applied: boolean; retiredCitations: CitationRetirement[]; }

/** Apply one reviewed migration against a complete legacy projection snapshot. */
export function applyTopicMigration(config: FlowConfig, migration: TopicMigration | undefined,
  existing: ReadonlyMap<string, string>, records: PublicationRecord[], sources: ReadonlyMap<string, PublicationSource> = new Map(),
  receipt?: RetirementReceipt): TopicMigrationResult {
  if (!migration) return { pages: new Map(existing), conflicts: [], applied: false, retiredCitations: [] };
  validateManifest(migration);
  const byId = new Map(records.map(record => [record.id, record]));
  if (byId.size !== records.length) throw new Error("duplicate publication identity");
  requireBasisRecords(migration, byId);
  const extras = records.filter(record => !migration.basisRecordIds.includes(record.id) && rendersAsFragment(record));
  const heldExtras = extras.filter(record => touchesMigration(record, migration.pages)
    || record.payload.claims.some(claim => migration.retiredPages?.some(page => claim.targetPageId === page.pageId)));
  const conflicts = heldExtras.map(record => ({
    claimRefs: record.payload.claims.map((_claim, index) => `${record.id}:${index}`), recordIds: [record.id],
    reason: "topic migration has extra legacy records for the same reviewed object; hold for re-review" }));
  const { writes, removals } = planMigrationPages(config, migration, existing, sources);
  const result = new Map(existing);
  for (const [id, body] of writes) result.set(id, body);
  for (const id of removals) result.delete(id);
  const retiredCitations = applyPageRetirements(config, migration.retiredPages ?? [], result, receipt);
  return { pages: result, conflicts, applied: true, retiredCitations };
}

function requireBasisRecords(migration: TopicMigration, byId: ReadonlyMap<string, PublicationRecord>): void {
  if (migration.basisRecordIds.some(id => !byId.has(id))) throw new Error("topic migration basis records are missing");
}

function planMigrationPages(config: FlowConfig, migration: TopicMigration, existing: ReadonlyMap<string, string>, sources: ReadonlyMap<string, PublicationSource>): { writes: Map<string, string>; removals: Set<string> } {
  const { pages, basisRecordIds } = migration;
  const writes = new Map<string, string>(); const removals = new Set<string>();
  for (const page of pages) {
    if (migrationAlreadyApplied(page, basisRecordIds, existing)) continue;
    const target = validatePage(config, page); const previous = readPreviousPages(page, existing, config);
    validateTargetOwner(target, page, existing);
    const priorBodies = previous.map(item => item.body); validateCitationChanges(priorBodies, page.body, page.citationRetirements);
    writes.set(target, retirePageProvenance(renderMigrationPage(page, existing.get(target) ?? priorBodies[0], priorBodies, basisRecordIds), page.citationRetirements, sources));
    for (const item of page.previousPages) if (item.pageId !== target) removals.add(item.pageId);
  }
  return { writes, removals };
}

function migrationAlreadyApplied(page: TopicMigrationPage, basisRecordIds: string[], existing: ReadonlyMap<string, string>): boolean {
  const target = existing.get(page.pageId); if (!target) return false;
  const metadata = parseFrontmatter(target).meta;
  const applied = metadata.knowledgeMigrationBasisRecordIds;
  return metadata.knowledgeMigrationPageHash === sha256Text(JSON.stringify(page)) && metadata.knowledgeMigrationTopicId === page.topicId && Array.isArray(applied)
    && JSON.stringify([...applied].sort()) === JSON.stringify([...basisRecordIds].sort());
}

function validateTargetOwner(target: string, page: TopicMigrationPage, existing: ReadonlyMap<string, string>): void {
  const targetBody = existing.get(target); if (!targetBody || page.previousPages.some(item => item.pageId === target)) return;
  throw new Error("topic migration target must be listed in previousPages");
}

function validateManifest(migration: TopicMigration): void {
  if (!validMigrationHeader(migration)) throw new Error("topic migration manifest is invalid");
  const seen = new Set<string>(); const seenPrevious = new Set<string>();
  for (const page of migration.pages) {
    validateMigrationPage(page, seen);
    for (const previous of page.previousPages) {
      if (seenPrevious.has(previous.pageId)) throw new Error("topic migration previous page is assigned twice");
      seenPrevious.add(previous.pageId);
    }
  }
  validatePageRetirements(migration.retiredPages, new Set([...seen, ...seenPrevious]));
}

function validateMigrationPage(page: TopicMigrationPage, seen: Set<string>): void {
  if (seen.has(page.pageId)) throw new Error("topic migration target is duplicated");
  seen.add(page.pageId);
  if (!isId(page.pageId)) throw new Error("topic migration page path is invalid");
  if (!HASH.test(page.topicId) || typeof page.body !== "string" || !page.body.trim() || /^---\r?\n/.test(page.body)) throw new Error("topic migration page is invalid");
  if (!validPreviousPages(page.previousPages)) throw new Error("topic migration previous page is invalid");
  validateRetirementShape(page.citationRetirements, page.body);
}

function validPreviousPages(previous: TopicMigrationPage["previousPages"]): boolean {
  return Array.isArray(previous) && new Set(previous.map(item => item.pageId)).size === previous.length
    && previous.every(item => isId(item.pageId) && HASH.test(item.sha256));
}

function validMigrationHeader(migration: TopicMigration): boolean {
  return migration.version === 1 && Array.isArray(migration.basisRecordIds)
    && new Set(migration.basisRecordIds).size === migration.basisRecordIds.length
    && migration.basisRecordIds.every(isRecordId) && Array.isArray(migration.pages)
    && (migration.pages.length > 0 || Boolean(migration.retiredPages?.length));
}

function validatePage(config: FlowConfig, page: TopicMigrationPage): string {
  validateMigrationIdentity(page); validateTargetOwnership(config, page);
  return page.pageId;
}

function validateMigrationIdentity(page: TopicMigrationPage): void {
  if (typeof page.projectId !== "string" || !page.projectId || typeof page.projectLabel !== "string" || !page.projectLabel
      || typeof page.topic !== "string" || !page.topic || typeof page.decisionObject !== "string") throw new Error("topic migration identity is invalid");
}

function validateTargetOwnership(config: FlowConfig, page: TopicMigrationPage): void {
  const owners = projectOwners(config, page.pageId);
  if (owners.length > 1 || owners.length === 1 && owners[0] !== page.projectId) throw new Error("topic migration target is outside project ownership");
}

function readPreviousPages(page: TopicMigrationPage, existing: ReadonlyMap<string, string>, config: FlowConfig): Array<{ pageId: string; body: string }> {
  return page.previousPages.map(item => {
    const body = existing.get(item.pageId); if (body === undefined || sha256Text(body) !== item.sha256) throw new Error("topic migration user page hash mismatch");
    const projectId = parseFrontmatter(body).meta.projectId;
    if (typeof projectId === "string" && projectId !== page.projectId) throw new Error("topic migration crosses project boundary");
    const owners = projectOwners(config, item.pageId);
    if (owners.length > 1 || owners.length === 1 && owners[0] !== page.projectId) throw new Error("topic migration page is outside project ownership");
    return { pageId: item.pageId, body };
  });
}

function renderMigrationPage(page: TopicMigrationPage, base: string | undefined, priorBodies: string[], basisRecordIds: string[]): string {
  const prior = parseFrontmatter(base ?? ""); const priorMeta = prior.meta;
  const sources = [...new Set(priorBodies.flatMap(body => strings(parseFrontmatter(body).meta.sources)).concat(strings(priorMeta.sources)))];
  const refs = [...new Set(priorBodies.flatMap(body => strings(parseFrontmatter(body).meta.knowledgePublicationRefs)).concat(strings(priorMeta.knowledgePublicationRefs)))];
  const ids = [...new Set(priorBodies.flatMap(body => strings(parseFrontmatter(body).meta.knowledgeClaimIds)).concat(strings(priorMeta.knowledgeClaimIds)))];
  const tags = [...new Set([...strings(priorMeta.tags), page.projectId, page.topic, page.title].filter(Boolean))];
  const metadata = { ...priorMeta, title: page.title, summary: firstParagraph(page.body).slice(0, 240), projectId: page.projectId,
    projectLabel: page.projectLabel, knowledgeTopicId: page.topicId, knowledgeTopic: page.topic,
    knowledgeDecisionObject: page.decisionObject, knowledgeMigrationTopicId: page.topicId,
    knowledgeMigrationPageHash: sha256Text(JSON.stringify(page)),
    knowledgeMigrationBasisRecordIds: [...basisRecordIds].sort(), sources, knowledgePublicationRefs: refs, knowledgeClaimIds: ids, tags,
    ...(priorMeta.updatedAt ? { updatedAt: priorMeta.updatedAt } : {}) };
  return `${buildFrontmatter(metadata)}\n\n${page.body.trimEnd()}\n`;
}

function touchesMigration(record: PublicationRecord, pages: TopicMigrationPage[]): boolean {
  return record.payload.claims.some(claim => pages.some(page => claim.targetPageId === page.pageId ||
    record.payload.projectId === page.projectId && normalize(claim.topic) === normalize(page.topic) && normalize(claim.decisionObject ?? "") === normalize(page.decisionObject)));
}
function strings(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }
function normalize(value: string): string { return value.normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase(); }
export function projectOwners(config: FlowConfig, pageId: string): string[] {
  return Object.entries(config.projects ?? {}).filter(([, project]) => project.pages?.includes(pageId)).map(([id]) => id);
}
function firstParagraph(body: string): string { return body.split(/\n\s*\n/).find(line => line.trim() && !line.trim().startsWith("#"))?.trim() ?? body.trim(); }
function isId(value: unknown): value is string { const parsed = typeof value === "string" ? parseQualifiedPageId(value) : null; return Boolean(parsed && parsed.namespace === "concepts"); }
function isRecordId(value: unknown): value is string { return typeof value === "string" && HASH.test(value); }
