/**
 * Deterministic topic routing for immutable publications. Semantic matching is
 * reviewed at intake; replay uses explicit targets or exact canonical metadata.
 * Missing/ambiguous destinations are held rather than guessed from keywords.
 */
import { sha256Text } from "../../src/connectors/hash.js";
import { parseFrontmatter, slugify } from "../../src/utils/markdown.js";
import { acceptedPublicationClaims } from "./publication-validation.js";
import type { FlowConfig } from "./types.js";
import type { PublicationConflict, PublicationEntry, PublicationRecord, TopicPage } from "./publication-types.js";
import { rendersAsFragment } from "./publication-types.js";

/** Canonical keys ignore whitespace/case, but do not infer semantic synonyms. */
function normalized(value: string): string {
  return value.normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase();
}

/** Include the decision object so matching a broad topic cannot merge platforms. */
function topicKey(project: string, topic: string, object: string): string {
  return JSON.stringify([project, normalized(topic), normalized(object)]);
}

/** Validate every publication before making any derived file visible. */
export function publicationEntries(records: PublicationRecord[]): PublicationEntry[] {
  return orderedRecords(records).flatMap(record => {
    if (!rendersAsFragment(record)) return [];
    const value = record.payload;
    const targets = value.claims.flatMap(claim => claim.targetPageId ? [claim.targetPageId] : []);
    const claims = acceptedPublicationClaims(record, targets);
    return claims.map((claim, index) => ({ ref: `${record.id}:${index}`, record, claim, index }));
  });
}

/** Causal parents precede their updates; ties use stable text ordering, never locale. */
function orderedRecords(records: PublicationRecord[]): PublicationRecord[] {
  const pending = new Map(records.map(record => [record.id, record]));
  if (pending.size !== records.length) throw new Error("duplicate publication identity");
  const result: PublicationRecord[] = [];
  while (pending.size) {
    const ready = [...pending.values()].filter(record => !record.payload.basisRecordIds.some(id => pending.has(id)))
      .sort((a, b) => compare(a.payload.createdAt, b.payload.createdAt) || compare(a.id, b.id));
    if (!ready.length) throw new Error("cyclic publication basis");
    for (const record of ready) { result.push(record); pending.delete(record.id); }
  }
  return result;
}

function compare(left: string, right: string): number { return left < right ? -1 : left > right ? 1 : 0; }

function concurrentVariants(left: PublicationEntry, right: PublicationEntry): boolean {
  if (left.routingGroup && left.routingGroup === right.routingGroup) return false;
  const meaning = ({ claim: c }: PublicationEntry) => JSON.stringify([c.kind, normalized(c.text), c.status, normalized(c.useWhen)]);
  return left.record.id !== right.record.id && meaning(left) !== meaning(right)
    && !left.record.payload.basisRecordIds.includes(right.record.id)
    && !right.record.payload.basisRecordIds.includes(left.record.id);
}

/** Different unobserved contributions remain outside retrieval until reviewed together. */
function groupConflicts(groups: PublicationEntry[][]): PublicationConflict[] {
  const output = new Map<string, PublicationConflict>();
  for (const group of groups) {
    const blocked = group.filter(left => group.some(right => concurrentVariants(left, right)));
    if (!blocked.length) continue;
    const claimRefs = [...new Set(blocked.map(entry => entry.ref))].sort();
    output.set(claimRefs.join(","), { claimRefs, recordIds: [...new Set(blocked.map(entry => entry.record.id))].sort(),
      reason: "concurrent-topic-variants: independent reviews did not observe each other; needs review" });
  }
  return [...output.values()].sort((a, b) => compare(a.claimRefs.join(), b.claimRefs.join()));
}

/** Structural conflict checks are available without reading a materialization root. */
export function entryConflicts(entries: PublicationEntry[]): PublicationConflict[] {
  const groups = new Map<string, PublicationEntry[]>();
  for (const entry of entries) {
    const { claim, record: { payload } } = entry;
    const keys = [topicKey(payload.projectId, claim.topic, claim.decisionObject ?? "")];
    if (claim.targetPageId) keys.push(JSON.stringify([payload.projectId, "target", claim.targetPageId]));
    for (const key of keys) groups.set(key, [...(groups.get(key) ?? []), entry]);
  }
  return groupConflicts([...groups.values()]);
}

function projectPrefix(projectId: string): string {
  const readable = projectId.replace(/[^A-Za-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 32) || "project";
  return `${readable}-${sha256Text(projectId).slice(0, 8)}`;
}

function baselinePages(existing: ReadonlyMap<string, string>, config: FlowConfig): Map<string, TopicPage> {
  return new Map([...existing].map(([id, original]) => {
    const { meta } = parseFrontmatter(original);
    const owners = Object.entries(config.projects ?? {}).filter(([, project]) => project.pages?.includes(id)).map(([key]) => key);
    const projectId = typeof meta.projectId === "string" ? meta.projectId : owners.length === 1 ? owners[0] : "";
    return [id, { id, original, projectId, topic: typeof meta.knowledgeTopic === "string" ? meta.knowledgeTopic : "",
      decisionObject: typeof meta.knowledgeDecisionObject === "string" ? meta.knowledgeDecisionObject : "", entries: [] }];
  }));
}

function newPage(entry: PublicationEntry): TopicPage {
  const { claim, record: { payload } } = entry;
  const object = claim.decisionObject ?? "";
  const identity = topicKey(payload.projectId, claim.topic, object);
  const readable = slugify(`${claim.topic} ${object}`).slice(0, 75).replace(/-$/, "") || claim.slug;
  return { id: `concepts/${projectPrefix(payload.projectId)}-${readable}-${sha256Text(identity).slice(0, 12)}`,
    projectId: payload.projectId, topic: claim.topic, decisionObject: object, entries: [] };
}

function matches(entry: PublicationEntry, page: TopicPage): boolean {
  return page.projectId === entry.record.payload.projectId && Boolean(page.topic)
    && topicKey(page.projectId, page.topic, page.decisionObject)
      === topicKey(entry.record.payload.projectId, entry.claim.topic, entry.claim.decisionObject ?? "");
}

function resolvePage(entry: PublicationEntry, pages: Map<string, TopicPage>, aliases: Map<string, string>): TopicPage | undefined {
  const target = entry.claim.targetPageId;
  const destination = target ? pages.get(aliases.get(target) ?? target) : undefined;
  if (target && !legacyRelatedPage(entry, destination)) return explicitPage(entry, destination);
  const matching = [...pages.values()].filter(page => matches(entry, page));
  if (matching.length > 1) return;
  if (matching.length === 1) return matching[0];
  const created = newPage(entry);
  if (pages.has(created.id)) return;
  pages.set(created.id, created);
  return created;
}

/** Old targets were rendered as related-page hints, not reviewed object identity. */
function legacyRelatedPage(entry: PublicationEntry, page: TopicPage | undefined): boolean {
  if (!page || page.projectId !== entry.record.payload.projectId) return false;
  return !entry.claim.decisionObject && normalized(entry.claim.topic) !== normalized(page.topic);
}

function explicitPage(entry: PublicationEntry, page: TopicPage | undefined): TopicPage | undefined {
  if (!page || page.projectId !== entry.record.payload.projectId) return;
  const object = entry.claim.decisionObject ?? "";
  if (object && page.decisionObject && normalized(object) !== normalized(page.decisionObject)) return;
  if (!page.topic) page.topic = entry.claim.topic;
  if (!page.decisionObject) page.decisionObject = object;
  return page;
}

/** Route accepted segments; legacy record ids remain aliases, never new user pages. */
export function planTopicPages(config: FlowConfig, entries: PublicationEntry[], existing: ReadonlyMap<string, string>): {
  pages: TopicPage[]; conflicts: PublicationConflict[];
} {
  const pages = baselinePages(existing, config); const aliases = new Map<string, string>();
  const conflicts = entryConflicts(entries);
  for (const entry of entries) {
    const page = resolvePage(entry, pages, aliases);
    if (!page) {
      conflicts.push({ claimRefs: [entry.ref], recordIds: [entry.record.id], reason: "topic-target-missing-ambiguous-or-out-of-scope: needs review" });
      continue;
    }
    page.entries.push(entry);
    aliases.set(`concepts/${projectPrefix(entry.record.payload.projectId)}-record-${entry.record.id.slice(0, 32)}-${entry.index}`, page.id);
  }
  conflicts.push(...groupConflicts([...pages.values()].map(page => page.entries)));
  const unique = [...new Map(conflicts.map(conflict => [conflict.claimRefs.join(","), conflict])).values()];
  const blocked = new Set(unique.flatMap(conflict => conflict.claimRefs));
  for (const page of pages.values()) page.entries = page.entries.filter(entry => !blocked.has(entry.ref));
  return { pages: [...pages.values()].filter(page => page.entries.length), conflicts: unique };
}
