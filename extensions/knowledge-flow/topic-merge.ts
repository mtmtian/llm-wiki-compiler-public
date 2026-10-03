/**
 * Reviewed merges of revision-layer topic pages.
 *
 * Most topic pages are created and updated by whole-page revision records, and every replica generation
 * replays all records from the frozen baseline. A merge therefore cannot simply delete pages: the next
 * replay would recreate them from their own creation records. Instead the replay is split around the
 * merges (materialize-revisions.ts):
 *
 * 1. records that do not touch a merged page, and the records a merge absorbed, rebuild the previous
 *    pages exactly as the merge was reviewed (each page's bytes must match its reviewed SHA-256);
 * 2. each merge replaces its surviving page with the reviewed body, removes the other pages and points
 *    their links at the survivor; every previous citation must stay or be explicitly retired;
 * 3. later records then apply to the merged page; a later record that still targets a removed page is
 *    held for review rather than recreating it.
 *
 * The merged page is a semantic page whose provenance (sources, publication refs, claim IDs, source
 * projects and tags) is the union of the previous pages. Any mismatch fails the generation closed.
 */
import { buildFrontmatter, parseFrontmatter } from "../../src/utils/markdown.js";
import { sha256Text } from "../../src/connectors/hash.js";
import { sourceProjectIds } from "../../src/utils/topic-scope.js";
import { validateCitationChanges } from "./citation-retirement.js";
import { retirePageProvenance } from "./retirement-projection.js";
import { firstParagraph, strings } from "./materialize-render.js";
import type { PublicationSource } from "./materialize-render.js";
import type { PublicationConflict, PublicationRecord } from "./publication-types.js";
import type { TopicMerge, TopicMigrationPreviousPage } from "./topic-revision-types.js";

export interface AppliedMerges {
  /** Removed page id → surviving page id, for link rewriting and source ownership. */
  links: Map<string, string>;
  /** Pages the merges removed. */
  removed: TopicMigrationPreviousPage[];
  /** Surviving pages, in manifest order. */
  pageIds: string[];
}

/** Split records into those replayed before the merges and those that apply to the merged pages. */
export function partitionAroundMerges(records: PublicationRecord[], merges: readonly TopicMerge[]):
  { before: PublicationRecord[]; after: PublicationRecord[] } {
  if (!merges.length) return { before: records, after: [] };
  const managed = new Set(merges.flatMap(merge => merge.previousPages.map(page => page.pageId)));
  const absorbed = new Set(merges.flatMap(merge => merge.absorbedRecordIds));
  const after = new Set(records.filter(record => !absorbed.has(record.id)
    && (record.payload.topicRevisions ?? []).some(revision => managed.has(revision.pageId))).map(record => record.id));
  // A record built on a later record replays after it as well, so a hold on the later record still propagates.
  for (let grew = true; grew;) {
    grew = false;
    for (const record of records) {
      if (after.has(record.id) || absorbed.has(record.id) || !record.payload.basisRecordIds?.some(id => after.has(id))) continue;
      after.add(record.id); grew = true;
    }
  }
  return { before: records.filter(record => !after.has(record.id)), after: records.filter(record => after.has(record.id)) };
}

/** Hold later records that revise a page a merge removed; the others continue to the replay. */
export function holdRevisionsOfMergedPages(records: PublicationRecord[], merges: readonly TopicMerge[]):
  { records: PublicationRecord[]; conflicts: PublicationConflict[] } {
  const removed = new Map(merges.flatMap(merge => merge.previousPages
    .filter(page => page.pageId !== merge.pageId).map(page => [page.pageId, merge.pageId] as const)));
  const kept: PublicationRecord[] = []; const conflicts: PublicationConflict[] = [];
  for (const record of records) {
    const revisions = record.payload.topicRevisions ?? [];
    const target = revisions.map(revision => removed.get(revision.pageId)).find(Boolean);
    if (!target) { kept.push(record); continue; }
    conflicts.push({ claimRefs: revisions.flatMap(revision => revision.claimIndexes.map(index => `${record.id}:${index}`)).sort(),
      recordIds: [record.id], reason: `topic page was merged into ${target}; revise the merged page instead` });
  }
  return { records: kept, conflicts };
}

/** Apply every merge to `desired`; throws when a previous page differs from its reviewed bytes. */
export function applyTopicMerges(merges: readonly TopicMerge[], desired: Map<string, string>,
  sources: ReadonlyMap<string, PublicationSource>): AppliedMerges {
  const applied: AppliedMerges = { links: new Map(), removed: [], pageIds: [] };
  for (const merge of merges) {
    if (!merge.previousPages.some(page => page.pageId === merge.pageId)) throw new Error("topic merge target must be one of its previous pages");
    const prior = merge.previousPages.map(page => reviewedPage(desired, page));
    validateCitationChanges(prior, merge.body, merge.citationRetirements);
    desired.set(merge.pageId, retirePageProvenance(renderMergedPage(merge, prior), merge.citationRetirements, sources));
    for (const page of merge.previousPages.filter(item => item.pageId !== merge.pageId)) {
      desired.delete(page.pageId); applied.links.set(page.pageId, merge.pageId); applied.removed.push(page);
    }
    applied.pageIds.push(merge.pageId);
  }
  return applied;
}

function reviewedPage(desired: ReadonlyMap<string, string>, page: TopicMigrationPreviousPage): string {
  const body = desired.get(page.pageId);
  if (body === undefined || sha256Text(body) !== page.sha256) throw new Error(`topic merge previous page differs from its reviewed bytes: ${page.pageId}`);
  return body;
}

/** The surviving page keeps its identity; provenance becomes the union of every previous page. */
function renderMergedPage(merge: TopicMerge, prior: string[]): string {
  const metas = prior.map(body => parseFrontmatter(body).meta);
  const target = metas[merge.previousPages.findIndex(page => page.pageId === merge.pageId)];
  const union = (key: string) => [...new Set(metas.flatMap(meta => strings(meta[key])))];
  const created = metas.map(meta => meta.createdAt).filter((value): value is string => typeof value === "string").sort()[0];
  const { projectId: _projectId, projectLabel: _projectLabel, ...identity } = target;
  const meta: Record<string, unknown> = { ...identity, title: merge.title, summary: firstParagraph(merge.body).slice(0, 240),
    knowledgeTopic: merge.topic, knowledgeDecisionObject: merge.decisionObject,
    ...(created ? { createdAt: created } : {}), updatedAt: merge.mergedAt,
    sources: union("sources"), knowledgeClaimIds: union("knowledgeClaimIds"),
    knowledgePublicationRefs: union("knowledgePublicationRefs"),
    tags: [...new Set([...union("tags"), merge.topic, merge.title])],
    topicScope: "semantic", sourceProjectIds: [...new Set(metas.flatMap(meta => sourceProjectIds(meta)))].sort(),
    knowledgeMergedPages: merge.previousPages.map(page => page.pageId).sort() };
  return `${buildFrontmatter(meta)}\n\n${merge.body.trimEnd()}\n`;
}
