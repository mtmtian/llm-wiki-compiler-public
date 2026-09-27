/**
 * Keep a curated projection's metadata and generated quote bundles consistent
 * with its surviving prose. Only explicitly retired publication evidence is
 * eligible for pruning; frozen source material and unrelated files stay intact.
 */
import path from "node:path";
import { readFile, readdir, unlink } from "node:fs/promises";
import { buildFrontmatter, extractCitations, parseFrontmatter } from "../../src/utils/markdown.js";
import { readState, writeState } from "../../src/utils/state.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { citationMarkers } from "./citation-retirement.js";
import type { CitationRetirement } from "./citation-retirement.js";
import type { PublicationSource } from "./materialize-render.js";
import type { WikiState } from "../../src/utils/types.js";

/** Remove only provenance made obsolete by this explicitly reviewed edit. */
export function retirePageProvenance(text: string, retirements: CitationRetirement[] | undefined,
  sources: ReadonlyMap<string, PublicationSource>): string {
  if (!retirements?.length) return text;
  const { meta, body } = parseFrontmatter(text);
  const claimIds = resolveProvenance(meta, sources);
  const surviving = new Set(citationMarkers(body));
  const removedRefs = new Set<string>();
  for (const source of sources.values()) for (const [ref, markers] of source.citations) {
    if (markers.some(marker => retirements.some(item => item.citation === marker)) && !markers.some(marker => surviving.has(marker))) {
      removedRefs.add(ref);
    }
  }
  const retainedIds = new Set(metadataStrings(meta.knowledgePublicationRefs).filter(ref => !removedRefs.has(ref)).map(ref => claimIds.get(ref)));
  const removedIds = new Set([...removedRefs].flatMap(ref => {
    const id = claimIds.get(ref); return id && !retainedIds.has(id) ? [id] : [];
  }));
  const retiredNames = new Set(retirements.flatMap(item => extractCitations(item.citation)));
  const retainedNames = new Set(extractCitations(body));
  const filter = (value: unknown, removed: ReadonlySet<string>): unknown => Array.isArray(value)
    ? value.filter(item => typeof item !== "string" || !removed.has(item)) : value;
  const droppedNames = new Set([...retiredNames].filter(name => !retainedNames.has(name)));
  for (const [key, removed] of [["sources", droppedNames], ["knowledgePublicationRefs", removedRefs], ["knowledgeClaimIds", removedIds]] as const) {
    if (key in meta) meta[key] = filter(meta[key], removed);
  }
  return `${buildFrontmatter(meta)}\n\n${body.trimEnd()}\n`;
}

/** Baseline metadata has no positional ref-to-claim mapping; never guess one from quote text. */
function resolveProvenance(meta: Record<string, unknown>, sources: ReadonlyMap<string, PublicationSource>): Map<string, string> {
  const claims = new Map([...sources.values()].flatMap(source => [...source.claimIds]));
  const knownIds = new Set(claims.values());
  if (metadataStrings(meta.knowledgePublicationRefs).some(ref => !claims.has(ref)) ||
    metadataStrings(meta.knowledgeClaimIds).some(id => !knownIds.has(id))) {
    throw new Error("citation retirement provenance is unavailable from original publications; needs review");
  }
  return claims;
}

function metadataStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** Prune uncited generated bundles after all revisions; replay applies the same decision. */
export async function pruneRetiredSources(root: string, retirements: CitationRetirement[],
  sources: ReadonlyMap<string, PublicationSource>, protectedSources: ReadonlySet<string>): Promise<void> {
  if (!retirements.length) return;
  const names = new Set(retirements.flatMap(item => extractCitations(item.citation)));
  const wiki = await wikiContents(root, "wiki");
  const state = await readState(root); let changed = retireSourceOwnership(state, names, wiki);
  for (const source of sources.values()) {
    if (protectedSources.has(source.name) || !names.has(source.name) || [...wiki.values()].some(text => mentionsSource(text, source.name))) continue;
    const file = await confineUnderRoot(path.join("sources", source.name), root, { mustExist: true });
    if (await readFile(file, "utf8") !== source.content) throw new Error("retired publication source was modified");
    await unlink(file); delete state.sources[source.name]; changed = true;
  }
  if (changed) await writeState(root, state);
}

/** A surviving shared bundle owns only pages that still attribute evidence to it. */
function retireSourceOwnership(state: WikiState, retired: ReadonlySet<string>, wiki: ReadonlyMap<string, string>): boolean {
  let changed = false;
  for (const name of retired) {
    const entry = state.sources[name]; if (!entry) continue;
    const retained = entry.concepts.filter(slug => {
      const page = wiki.get(`wiki/concepts/${slug}.md`); if (page === undefined) return true;
      const { meta, body } = parseFrontmatter(page);
      return metadataStrings(meta.sources).includes(name) || extractCitations(body).includes(name);
    });
    if (retained.length !== entry.concepts.length) { entry.concepts = retained; changed = true; }
  }
  return changed;
}

/** Files supplied by the caller's frozen baseline are never pruned as new bundles. */
export async function existingSourceNames(root: string): Promise<Set<string>> {
  const files = await readdir(path.join(root, "sources")).catch(error => {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  });
  return new Set(files);
}

/** A reviewed deletion must also account for surviving links in every Wiki namespace. */
export async function validateRetiredPageLinks(root: string, desired: ReadonlyMap<string, string>, retiredIds: string[]): Promise<void> {
  if (!retiredIds.length) return;
  const retired = new Set(retiredIds.flatMap(id => [id, id.slice("concepts/".length)]));
  const bodies = [...desired.values(), ...(await wikiContents(root, "wiki", true)).values()];
  for (const body of bodies) {
    const wikiLinks = [...body.matchAll(/\[\[([^\]|]+)(?:\|[^\]]*)?\]\]/g)].map(match => match[1]);
    const markdownLinks = [...body.matchAll(/\]\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)/g)].map(match => match[1] ?? match[2]);
    if ([...wikiLinks, ...markdownLinks].some(target => retired.has(normalizedTarget(target)))) {
      throw new Error("retired page still has a surviving Wiki link; repair it in the reviewed migration");
    }
  }
}

function normalizedTarget(target: string): string {
  let value: string;
  try { value = decodeURIComponent(target); } catch { value = target; }
  return value.split("#")[0].replace(/^(?:\.\.\/|\.\/|wiki\/)+/, "").replace(/\.md$/, "").trim();
}

function mentionsSource(text: string, name: string): boolean {
  return text.includes(name) || text.includes(`sources/${name.slice(0, -3)}`);
}

async function wikiContents(root: string, relative: string, skipConcepts = false): Promise<Map<string, string>> {
  const folder = await confineUnderRoot(relative, root, { mustExist: true });
  const entries = await readdir(folder, { withFileTypes: true }); const texts = new Map<string, string>();
  for (const entry of entries) {
    const child = path.join(relative, entry.name);
    if (child === "wiki/index.md" || child === "wiki/MOC.md") continue;
    if (skipConcepts && child === "wiki/concepts") continue;
    if (entry.isDirectory()) {
      for (const [file, content] of await wikiContents(root, child, skipConcepts)) texts.set(file, content);
    } else if (entry.name.endsWith(".md")) texts.set(child, await readFile(await confineUnderRoot(child, root, { mustExist: true }), "utf8"));
  }
  return texts;
}
