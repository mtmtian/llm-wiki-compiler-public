/**
 * Replay of whole-page revision records, split around reviewed topic merges (topic-merge.ts).
 *
 * Records are applied in dependency order, each group only when its basis hash matches the current page.
 * With merges, the records that rebuild the merged pages replay first, then the merges replace those
 * pages, then the remaining records apply to the merged result. Without merges this is one ordinary pass.
 */
import { extractCitations, parseFrontmatter } from "../../src/utils/markdown.js";
import { planTopicRevisions, revisionBasisMatches } from "./topic-revision.js";
import { renderTopicRevisionPage } from "./materialize-render.js";
import type { PublicationSource } from "./materialize-render.js";
import type { PublicationConflict, PublicationRecord } from "./publication-types.js";
import type { FlowConfig } from "./types.js";
import { validateCitationChanges, validateRetirementReferences } from "./citation-retirement.js";
import { readPriorSources } from "./consolidation-sources.js";
import { applyTopicMerges, holdRevisionsOfMergedPages, partitionAroundMerges } from "./topic-merge.js";
import type { AppliedMerges } from "./topic-merge.js";
import { rewritePageLinks } from "./page-links.js";

export type RenderedRevision = { item: ReturnType<typeof planTopicRevisions>["applications"][number]; body: string };

export interface RevisionReplay {
  /** Revisions rendered before the merges; promoted first. */
  earlier: RenderedRevision[];
  merges: AppliedMerges;
  /** Each surviving page as the merge produced it (links already rewritten); promoted between the two passes. */
  merged: Array<{ pageId: string; body: string }>;
  /** Revisions rendered after the merges; promoted last. */
  later: RenderedRevision[];
  conflicts: PublicationConflict[];
}

/** Replay every revision record onto `desired`, applying reviewed merges between earlier and later records. */
export async function replayRevisions(config: FlowConfig, records: PublicationRecord[], desired: Map<string, string>,
  sources: ReadonlyMap<string, PublicationSource>): Promise<RevisionReplay> {
  const merges = config.topicMerges ?? [];
  const { before, after } = partitionAroundMerges(records, merges);
  const first = planTopicRevisions(before, desired);
  const conflicts = [...first.conflicts];
  const earlier = await renderRevisions(first.applications, desired, sources, conflicts, config.wikiRoot);
  const applied = applyTopicMerges(merges, desired, sources);
  rewritePageLinks(desired, applied.links);
  const merged = applied.pageIds.map(pageId => ({ pageId, body: desired.get(pageId)! }));
  const remaining = holdRevisionsOfMergedPages(after, merges);
  const second = planTopicRevisions(remaining.records, desired);
  conflicts.push(...remaining.conflicts, ...second.conflicts);
  const later = await renderRevisions(second.applications, desired, sources, conflicts, config.wikiRoot);
  return { earlier, merges: applied, merged, later, conflicts };
}

async function renderRevisions(applications: ReturnType<typeof planTopicRevisions>["applications"], desired: Map<string, string>,
  sources: ReadonlyMap<string, PublicationSource>, conflicts: PublicationConflict[], wikiRoot: string): Promise<Array<{ item: (typeof applications)[number]; body: string }>> {
  const rendered: Array<{ item: (typeof applications)[number]; body: string }> = [];
  const held = new Set(conflicts.flatMap(conflict => conflict.recordIds));
  const groups = new Map<string, typeof applications>();
  for (const item of applications) groups.set(item.record.id, [...(groups.get(item.record.id) ?? []), item]);
  for (const group of groups.values()) {
    const record = group[0].record;
    if (record.payload.basisRecordIds.some(id => held.has(id))) { holdRevisionGroup(group, conflicts, held, "revision depends on a held record"); continue; }
    const tentative = new Map(desired); const batch: Array<{ item: (typeof applications)[number]; body: string }> = [];
    if (group.some(item => !revisionBasisMatches(item.revision.basisHash, tentative.get(item.revision.pageId)))) {
      holdRevisionGroup(group, conflicts, held, "revision basis hash does not match current page"); continue;
    }
    const initial = new Map(tentative);
    const priorSources = await retirementSourcesForGroup(group, initial, sources, wikiRoot);
    for (const item of group) {
      const previous = tentative.get(item.revision.pageId);
      const body = renderReviewedRevision(item, previous, sources, priorSources);
      batch.push({ item: { ...item, previous }, body }); tentative.set(item.revision.pageId, body);
    }
    rendered.push(...batch); for (const item of batch) desired.set(item.item.revision.pageId, item.body);
  }
  return rendered;
}

async function retirementSourcesForGroup(group: ReturnType<typeof planTopicRevisions>["applications"], initial: ReadonlyMap<string, string>,
  sources: ReadonlyMap<string, PublicationSource>, wikiRoot: string): Promise<ReadonlyMap<string, string>> {
  if (!group.some(item => (item.revision.citationRetirements ?? []).some(retirement => retirement.replacement.startsWith("https:")))) {
    return new Map();
  }
  const names = [...new Set(group.flatMap(item => extractCitations(parseFrontmatter(initial.get(item.revision.pageId) ?? "").body)))];
  const generated = new Map([...sources.values()].filter(source => names.includes(source.name)).map(source => [source.name, source.content]));
  const missing = names.filter(name => !generated.has(name));
  for (const [name, content] of Object.entries(await readPriorSources(wikiRoot, missing))) generated.set(name, content);
  return generated;
}

function renderReviewedRevision(item: ReturnType<typeof planTopicRevisions>["applications"][number], previous: string | undefined,
  sources: ReadonlyMap<string, PublicationSource>, priorSources: ReadonlyMap<string, string>): string {
  const source = sources.get(item.record.id)!; const { revision } = item;
  const retirements = revision.citationRetirements ?? [];
  if (retirements.some(item => item.replacement.startsWith("https:"))) {
    validateRetirementReferences(retirements, [previous ?? "", ...item.record.payload.evidence.map(evidence => evidence.text),
      ...priorSources.values()]);
  }
  const body = renderTopicRevisionPage(revision, item.record, previous, source, sources);
  const resolvedRetirements = revision.citationRetirements?.map(value => ({ ...value, replacement: value.replacement.replace(
    /\{\{claim:(\d+)\}\}/g, (_match, index: string) => source.citations.get(`${item.record.id}:${index}`)?.[0] ?? "") }));
  validateCitationChanges([previous ?? ""], body, resolvedRetirements, { newMarkers: [...source.citations.values()].flat() });
  return body;
}

function holdRevisionGroup(group: ReturnType<typeof planTopicRevisions>["applications"], conflicts: PublicationConflict[], held: Set<string>, reason: string): void {
  const record = group[0]?.record; if (!record) return; held.add(record.id);
  conflicts.push({ claimRefs: group.flatMap(item => item.revision.claimIndexes.map(index => `${record.id}:${index}`)).sort(), recordIds: [record.id], reason });
}
