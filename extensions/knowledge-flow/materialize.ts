/**
 * Rebuild a disposable local Wiki from immutable publications. Publication
 * identity belongs to provenance; reviewed topic destinations own the pages.
 * The caller stages from the frozen baseline and exposes only a successful
 * generation. Shared projection separately checks ownership and human edits.
 */
import path from "node:path";
import { mkdir, readdir, readFile, unlink } from "node:fs/promises";
import { generateIndex, scanWikiPages } from "../../src/compiler/indexgen.js";
import { acquireLock, releaseLock } from "../../src/utils/lock.js";
import { refreshEmbeddingsDrainingPending } from "../../src/utils/embeddings-refresh.js";
import { qualifiedPageId } from "../../src/utils/page-id.js";
import { atomicWrite, parseFrontmatter } from "../../src/utils/markdown.js";
import { readState, writeState } from "../../src/utils/state.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { writeCandidate, listCandidates, deleteCandidate } from "../../src/compiler/candidates.js";
import { applyCandidateUnderLock } from "../../src/commands/review-approve.js";
import { sha256Text } from "../../src/connectors/hash.js";
import { entryConflicts, planTopicPages, publicationEntries } from "./materialize-plan.js";
import { canonicalTopicRoutes, routePublicationEntries } from "./topic-routes.js";
import { renderPublicationSources, renderTopicPage } from "./materialize-render.js";
import type { PublicationSource } from "./materialize-render.js";
import type { FlowConfig } from "./types.js";
import type { PublicationConflict, PublicationEntry, PublicationRecord, TopicPage } from "./publication-types.js";
import type { SourceState } from "../../src/utils/types.js";
import { planTopicRevisions, revisionEntries, validateTopicRevisionRecord } from "./topic-revision.js";
import { replayRevisions } from "./materialize-revisions.js";
import type { RenderedRevision, RevisionReplay } from "./materialize-revisions.js";
import { applyTopicMigration, projectOwners } from "./topic-migration.js";
import { generateProjectNavigation } from "./topic-navigation.js";
import { rewriteBody, rewritePageLinks } from "./page-links.js";
import { existingSourceNames, pruneRetiredSources, validateRetiredPageLinks } from "./retirement-projection.js";
import {
  createRetirementReceipt,
  matchesRetirementReceipt,
  readRetirementReceipt,
  writeRetirementReceipt,
} from "./retirement-receipt.js";
import type { RetirementReceipt } from "./retirement-receipt.js";

const MAX_TOPIC_PAGE_CHARS = 12_000;

type RenderedTopic = { page: TopicPage; body: string };
interface LegacyProjection {
  basisPlan: ReturnType<typeof planTopicPages>;
  extraPlan: ReturnType<typeof planTopicPages>;
  rendered: RenderedTopic[];
  sources: Map<string, PublicationSource>;
  desired: Map<string, string>;
  migration: ReturnType<typeof applyTopicMigration>;
  migrationPages: NonNullable<FlowConfig["topicMigration"]>["pages"];
  migrationLinks: Map<string, string>;
}

/** Return symmetric conflicts regardless of arrival or replay order. */
export function publicationConflicts(records: PublicationRecord[]): PublicationConflict[] {
  const legacy = entryConflicts(publicationEntries(records));
  const revisions = planTopicRevisions(records, new Map()).conflicts;
  return [...new Map([...legacy, ...revisions].map(conflict => [conflict.claimRefs.join(","), conflict])).values()];
}

async function readExistingPages(root: string): Promise<Map<string, string>> {
  const names = (await readdir(path.join(root, "wiki/concepts"))).filter(name => name.endsWith(".md")).sort();
  return new Map(await Promise.all(names.map(async name => {
    const file = await confineUnderRoot(path.join("wiki/concepts", name), root, { mustExist: true });
    return [`concepts/${name.slice(0, -3)}`, await readFile(file, "utf8")] as const;
  })));
}

async function rebuildLocalIndexes(config: FlowConfig): Promise<void> {
  await generateIndex(config.wikiRoot, null); await generateProjectNavigation(config);
  const pages = await scanWikiPages(path.join(config.wikiRoot, "wiki/concepts"));
  await refreshEmbeddingsDrainingPending(config.wikiRoot, pages.map(page => qualifiedPageId("concepts", page.slug)));
}

/** Replay uses no model and retains the compiler's candidate/approval boundary. */
export async function materializeRecords(config: FlowConfig, records: PublicationRecord[]): Promise<{ pages: number; conflicts: PublicationConflict[] }> {
  const entries = routePublicationEntries(publicationEntries(records), config.topicRoutes ?? []);
  await mkdir(path.join(config.wikiRoot, "wiki/concepts"), { recursive: true });
  if (!await acquireLock(config.wikiRoot)) throw new Error("local materialization is busy");
  try {
    const inputHash = await pinMaterializationInput(config, records);
    const existing = await readExistingPages(config.wikiRoot);
    const priorReceipt = config.topicMigration?.retiredPages?.length
      ? await readRetirementReceipt(config.wikiRoot) : null;
    const retirementReceipt = matchesRetirementReceipt(
      priorReceipt, inputHash, config.topicMigration,
    ) ? priorReceipt : undefined;
    const protectedSources = await existingSourceNames(config.wikiRoot);
    if ((await listCandidates(config.wikiRoot)).length) throw new Error("local materialization has pending candidates");
    const legacy = prepareLegacyProjection(config, entries, records, existing, retirementReceipt);
    validateRevisionProjectOwnership(config, records, legacy.desired);
    const replay = await replayRevisions(config, records, legacy.desired, legacy.sources);
    const links = new Map([...legacy.migrationLinks, ...replay.merges.links]);
    rewritePageLinks(legacy.desired, links);
    const rendered = withLinks(replay, links);
    await validateRetiredPageLinks(config.wikiRoot, legacy.desired, config.topicMigration?.retiredPages?.map(page => page.pageId) ?? []);
    await persistProjection(config, legacy, rendered, links);
    const persisted = await persistedSources(config.wikiRoot, legacy.sources);
    await pruneRetiredSources(config.wikiRoot, [...legacy.migration.retiredCitations,
      ...legacy.migrationPages.flatMap(page => page.citationRetirements ?? []),
      ...(config.topicMerges ?? []).flatMap(merge => merge.citationRetirements ?? []),
      ...[...rendered.earlier, ...rendered.later].flatMap(item => item.item.revision.citationRetirements ?? [])], persisted, protectedSources);
    await rebuildLocalIndexes(config);
    await writeRetirementReceipt(config.wikiRoot,
      retirementReceipt ?? createRetirementReceipt(inputHash, config.topicMigration, legacy.basisPlan.pages));
    const pages = (await readdir(path.join(config.wikiRoot, "wiki/concepts"))).filter(name => name.endsWith(".md")).length;
    return { pages, conflicts: [...legacy.basisPlan.conflicts, ...legacy.extraPlan.conflicts, ...legacy.migration.conflicts, ...replay.conflicts] };
  } finally { await releaseLock(config.wikiRoot); }
}

function prepareLegacyProjection(config: FlowConfig, entries: PublicationEntry[], records: PublicationRecord[], existing: Map<string, string>, receipt?: RetirementReceipt): LegacyProjection {
  const split = splitLegacyEntries(entries, config.topicMigration);
  const replayEntries = retiredReplayEntries(split.basis, existing, receipt);
  const replayRefs = new Set(replayEntries.map(entry => entry.ref));
  const basisPlan = planTopicPages(config, split.basis.filter(entry => !replayRefs.has(entry.ref)), existing);
  validateRevisionRecords(records); const revisionSourceEntries = revisionEntries(records);
  const sourceEntries = [...revisionSourceEntries, ...replayEntries];
  const basisSources = renderPublicationSources(basisPlan.pages, sourceEntries); const basisRendered = renderPages(basisPlan.pages, basisSources);
  const staged = applyMigrationStage(config, basisRendered, existing, records, basisSources, receipt);
  const extraPlan = planTopicPages(config, split.extra.filter(entry => !staged.blockedLegacy.has(entry.record.id)), staged.desired);
  const sources = renderPublicationSources([...basisPlan.pages, ...extraPlan.pages], sourceEntries);
  const rendered = [...renderPages(basisPlan.pages, sources), ...renderPages(extraPlan.pages, sources)];
  if (rendered.some(item => item.body.length > MAX_TOPIC_PAGE_CHARS)) throw new Error("topic page exceeds review budget; needs review");
  applyRenderedDesired(staged.desired, rendered, config.topicMigration, staged.migration.applied);
  return { basisPlan, extraPlan, rendered, sources, desired: staged.desired, migration: staged.migration,
    migrationPages: config.topicMigration?.pages ?? [], migrationLinks: staged.migrationLinks };
}

/** Recover missing retired entries solely to keep shared source bytes stable on replay. */
function retiredReplayEntries(
  entries: PublicationEntry[], existing: ReadonlyMap<string, string>, receipt?: RetirementReceipt,
): PublicationEntry[] {
  const refs = new Set((receipt?.retiredPages ?? []).filter(item => !existing.has(item.pageId))
    .flatMap(item => item.claimRefs));
  return entries.filter(entry => refs.has(entry.ref));
}

function applyRenderedDesired(desired: Map<string, string>, rendered: RenderedTopic[], migration: FlowConfig["topicMigration"], applied: boolean): void {
  const managed = applied ? migrationManagedIds(migration) : new Set<string>();
  for (const item of rendered) if (!managed.has(item.page.id)) desired.set(item.page.id, item.body);
}

function migrationManagedIds(migration: FlowConfig["topicMigration"]): Set<string> {
  return new Set([...(migration?.pages.flatMap(page => [page.pageId, ...page.previousPages.map(item => item.pageId)]) ?? []),
    ...(migration?.retiredPages?.map(page => page.pageId) ?? [])]);
}

function validateRevisionRecords(records: PublicationRecord[]): void {
  for (const record of records) if (record.payload.topicRevisions?.length) validateTopicRevisionRecord(record);
}

function applyMigrationStage(config: FlowConfig, basisRendered: RenderedTopic[], existing: Map<string, string>, records: PublicationRecord[], sources: ReadonlyMap<string, PublicationSource>, receipt?: RetirementReceipt): {
  desired: Map<string, string>; migration: ReturnType<typeof applyTopicMigration>; migrationLinks: Map<string, string>; blockedLegacy: Set<string>;
} {
  const desired = new Map(existing); for (const item of basisRendered) desired.set(item.page.id, item.body);
  validateRevisionProjectOwnership(config, records, desired);
  const migration = applyTopicMigration(config, config.topicMigration, desired, records, sources, receipt);
  desired.clear(); for (const [id, body] of migration.pages) desired.set(id, body);
  const migrationLinks = migration.applied ? migrationLinkMap(config.topicMigration?.pages ?? []) : new Map<string, string>(); rewritePageLinks(desired, migrationLinks);
  return { desired, migration, migrationLinks, blockedLegacy: new Set(migration.conflicts.flatMap(conflict => conflict.recordIds)) };
}

/** Rendered output with every moved page's links pointing at its survivor, in promotion order. */
function withLinks(replay: RevisionReplay, links: ReadonlyMap<string, string>): RevisionReplay {
  const rewrite = (items: RenderedRevision[]) => items.map(item => ({ ...item, body: rewriteBody(item.body, links) }));
  return { ...replay, earlier: rewrite(replay.earlier), later: rewrite(replay.later),
    merged: replay.merged.map(page => ({ ...page, body: rewriteBody(page.body, links) })) };
}

async function persistProjection(config: FlowConfig, legacy: LegacyProjection, rendered: RevisionReplay, links: ReadonlyMap<string, string>): Promise<void> {
  await writeAcceptedSources(config.wikiRoot, legacy, [...rendered.earlier, ...rendered.later]);
  await promoteTopics(config.wikiRoot, legacy);
  await promoteMigration(config, legacy);
  await promoteRevisions(config.wikiRoot, legacy.sources, rendered.earlier);
  await promoteMerges(config.wikiRoot, rendered);
  await promoteRevisions(config.wikiRoot, legacy.sources, rendered.later);
  await migrateSourceOwnership(config.wikiRoot, links, config.topicMigration?.retiredPages?.map(page => page.pageId) ?? []);
}

/** Write each merged page, then remove the pages it absorbed; the generation is private and freshly staged. */
async function promoteMerges(root: string, rendered: RevisionReplay): Promise<void> {
  for (const page of rendered.merged) await promoteMigrationPage(root, page.pageId, page.body);
  for (const page of rendered.merges.removed) {
    const relative = path.join("wiki", `${page.pageId}.md`);
    if (await readOptional(root, relative) !== null) await unlink(await confineUnderRoot(relative, root, { mustExist: true }));
  }
}

/** Keep incremental compilation and frozen-page protection on the reviewed canonical paths. */
async function migrateSourceOwnership(root: string, links: ReadonlyMap<string, string>, retiredPages: string[]): Promise<void> {
  if (links.size === 0 && !retiredPages.length) return;
  const state = await readState(root);
  const canonical = (slug: string): string => links.get(`concepts/${slug}`)?.slice("concepts/".length) ?? slug;
  const live = (slug: string): boolean => !retiredPages.includes(`concepts/${slug}`);
  for (const entry of Object.values(state.sources)) entry.concepts = [...new Set(entry.concepts.map(canonical).filter(live))];
  if (state.frozenSlugs) state.frozenSlugs = [...new Set(state.frozenSlugs.map(canonical).filter(live))];
  await writeState(root, state);
}

async function writeAcceptedSources(root: string, legacy: LegacyProjection, revisions: RenderedRevision[]): Promise<void> {
  const accepted = new Set(legacy.rendered.flatMap(item => item.page.entries.map(entry => entry.record.id)));
  for (const item of revisions) accepted.add(item.item.record.id);
  for (const [id, source] of legacy.sources) if (accepted.has(id)) await writeSource(root, source);
}

/** Pass only source bundles that exist after projection to retirement pruning. */
async function persistedSources(root: string, sources: ReadonlyMap<string, PublicationSource>): Promise<Map<string, PublicationSource>> {
  const persisted = new Map<string, PublicationSource>();
  for (const [id, source] of sources) if (await readOptional(root, path.join("sources", source.name)) !== null) persisted.set(id, source);
  return persisted;
}

async function promoteTopics(root: string, legacy: LegacyProjection): Promise<void> {
  for (const { page, body } of legacy.rendered) await promoteTopic(root, page, body, legacy.sources);
}

async function promoteMigration(config: FlowConfig, legacy: LegacyProjection): Promise<void> {
  const root = config.wikiRoot;
  if (!legacy.migration.applied) return;
  for (const page of legacy.migrationPages) await promoteMigrationPage(root, page.pageId, legacy.desired.get(page.pageId)!);
  await removeMigratedPages(root, [...legacy.migrationPages.flatMap(page => page.previousPages.filter(item => item.pageId !== page.pageId)),
    ...(config.topicMigration?.retiredPages ?? [])]);
}

async function promoteRevisions(root: string, sources: ReadonlyMap<string, PublicationSource>, revisions: RenderedRevision[]): Promise<void> {
  for (const { item, body } of revisions) await promoteRevision(root, item.revision.pageId, body, sources.get(item.record.id)!);
}

function splitLegacyEntries(entries: PublicationEntry[], migration: FlowConfig["topicMigration"]): { basis: PublicationEntry[]; extra: PublicationEntry[] } {
  if (!migration) return { basis: entries, extra: [] };
  const basis = new Set(migration.basisRecordIds);
  return { basis: entries.filter(entry => basis.has(entry.record.id)), extra: entries.filter(entry => !basis.has(entry.record.id)) };
}

function renderPages(pages: TopicPage[], sources: ReadonlyMap<string, PublicationSource>): Array<{ page: TopicPage; body: string }> {
  return pages.map(page => ({ page, body: renderTopicPage(page, sources) }));
}

function migrationLinkMap(pages: Array<{ pageId: string; previousPages: Array<{ pageId: string }> }>): Map<string, string> {
  const links = new Map<string, string>();
  for (const page of pages) for (const previous of page.previousPages) if (previous.pageId !== page.pageId) links.set(previous.pageId, page.pageId);
  return links;
}

function validateRevisionProjectOwnership(config: FlowConfig, records: PublicationRecord[], pages: ReadonlyMap<string, string>): void {
  for (const record of records) for (const revision of record.payload.topicRevisions ?? []) {
    validateRevisionOwner(config, record, revision.pageId, revision.topicScope, pages);
  }
}

function validateRevisionOwner(config: FlowConfig, record: PublicationRecord, pageId: string, topicScope: string | undefined,
  pages: ReadonlyMap<string, string>): void {
  const current = pages.get(pageId); const meta = current ? parseFrontmatter(current).meta : {};
  if (topicScope === "semantic" || meta.topicScope === "semantic") return;
  const projectId = meta.projectId;
  if (typeof projectId === "string" && projectId !== record.payload.projectId) throw new Error("topic revision crosses project boundary");
  const owners = projectOwners(config, pageId);
  if (owners.length > 1 || owners.length === 1 && owners[0] !== record.payload.projectId) throw new Error("topic revision page is outside project ownership");
}

/** Changed inputs require the caller's normal fresh-baseline staging path. */
async function pinMaterializationInput(config: FlowConfig, records: PublicationRecord[]): Promise<string> {
  const relative = ".llmwiki/materialization-input.json";
  const file = await confineUnderRoot(relative, config.wikiRoot, { mustExist: false });
  const projects = Object.entries(config.projects ?? {}).map(([id, project]) => [id, [...(project.pages ?? [])].sort()])
    .sort(([left], [right]) => String(left) < String(right) ? -1 : String(left) > String(right) ? 1 : 0);
  const identity = JSON.stringify({ recordIds: records.map(record => record.id).sort(), projects,
    routes: canonicalTopicRoutes(config.topicRoutes ?? []), migration: config.topicMigration ?? null,
    ...(config.topicScope === "semantic" ? { topicScope: "semantic" } : {}),
    ...(config.topicMerges?.length ? { merges: config.topicMerges } : {}) });
  const inputHash = sha256Text(identity);
  const content = JSON.stringify({ hash: inputHash }) + "\n";
  const prior = await readFile(file, "utf8").catch(error => {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  });
  if (prior !== null && prior !== content) throw new Error("changed materialization inputs require a fresh baseline root");
  await atomicWrite(path.join(config.wikiRoot, relative), content, { confineRoot: config.wikiRoot });
  return inputHash;
}

async function writeSource(root: string, source: PublicationSource): Promise<void> {
  const current = await readOptional(root, path.join("sources", source.name));
  if (current !== null && current !== source.content) throw new Error("publication source changed before materialization");
  await atomicWrite(path.join(root, "sources", source.name), source.content, { confineRoot: root });
}

async function readOptional(root: string, relative: string): Promise<string | null> {
  const file = await confineUnderRoot(relative, root, { mustExist: false });
  return readFile(file, "utf8").catch(error => {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  });
}

async function promoteTopic(root: string, page: TopicPage, body: string, sources: ReadonlyMap<string, PublicationSource>): Promise<void> {
  if (body === page.original) return;
  const { meta } = parseFrontmatter(body); const slug = page.id.slice("concepts/".length);
  const bundles = [...new Set(page.entries.map(entry => entry.record.id))].map(id => sources.get(id)!);
  const sourceStates = Object.fromEntries(bundles.map(source => [source.name, { hash: source.hash,
    concepts: [slug], compiledAt: page.entries.at(-1)!.record.payload.createdAt }]));
  await promoteBody(root, page.id, body, sourceStates, "topic candidate could not be approved");
}

async function promoteRevision(root: string, pageId: string, body: string, source: PublicationSource): Promise<void> {
  const { meta } = parseFrontmatter(body); const slug = pageId.slice("concepts/".length);
  const sourceStates = { [source.name]: { hash: source.hash, concepts: [slug], compiledAt: String(meta.updatedAt ?? "") } };
  await promoteBody(root, pageId, body, sourceStates, "topic revision could not be approved");
}

async function promoteMigrationPage(root: string, pageId: string, body: string): Promise<void> {
  const { meta } = parseFrontmatter(body); const slug = pageId.slice("concepts/".length);
  const sources = Array.isArray(meta.sources) ? meta.sources.filter((item): item is string => typeof item === "string") : [];
  await promoteBody(root, pageId, body, {}, "topic migration could not be approved", sources);
}

async function promoteBody(root: string, pageId: string, body: string, sourceStates: Record<string, SourceState>, failure: string,
  sourceNames?: string[]): Promise<void> {
  const { meta } = parseFrontmatter(body); const slug = pageId.slice("concepts/".length);
  const candidate = await writeCandidate(root, { title: String(meta.title), slug, summary: String(meta.summary ?? ""),
    sources: sourceNames ?? (Array.isArray(meta.sources) ? meta.sources.filter((item): item is string => typeof item === "string") : []),
    body, sourceStates, reviewMode: "policy", heldReasons: [{ code: "manual-review-requested" }] });
  if (!await applyCandidateUnderLock(root, candidate.id, {})) throw new Error(failure);
  const file = await confineUnderRoot(path.join("wiki", `${pageId}.md`), root, { mustExist: true });
  if (await readFile(file, "utf8") !== body) throw new Error(`${failure}: body mismatch`);
  await deleteCandidate(root, candidate.id);
}

async function removeMigratedPages(root: string, pages: Array<{ pageId: string; sha256: string }>): Promise<void> {
  for (const previous of pages) {
    const relative = path.join("wiki", `${previous.pageId}.md`);
    const current = await readOptional(root, relative);
    if (current !== null && sha256Text(current) !== previous.sha256) throw new Error("topic migration old page changed before removal");
    if (current !== null) await unlink(await confineUnderRoot(relative, root, { mustExist: true }));
  }
}
