/**
 * Render topic pages from independently accepted segments without rewriting
 * baseline prose. One publication supplies one quote bundle, with exact line
 * citations and per-paragraph authority, dates, applicability and rationale.
 */
import { sha256Text } from "../../src/connectors/hash.js";
import { buildFrontmatter, parseFrontmatter } from "../../src/utils/markdown.js";
import { sourceProjectIds } from "../../src/utils/topic-scope.js";
import { claimIdentity } from "./claim-identity.js";
import type { PublicationEntry, PublicationRecord, TopicPage } from "./publication-types.js";
import type { TopicRevision } from "./topic-revision-types.js";
import { retirePageProvenance } from "./retirement-projection.js";

export interface PublicationSource {
  name: string;
  content: string;
  hash: string;
  citations: Map<string, string[]>;
  claimIds: Map<string, string>;
}

/** Quote bundles contain only entries allowed into the accepted view. */
export function renderPublicationSources(pages: TopicPage[], extraEntries: PublicationEntry[] = []): Map<string, PublicationSource> {
  const groups = new Map<string, PublicationEntry[]>();
  for (const page of pages) for (const entry of page.entries) {
    groups.set(entry.record.id, [...(groups.get(entry.record.id) ?? []), entry]);
  }
  for (const entry of extraEntries) groups.set(entry.record.id, [...(groups.get(entry.record.id) ?? []), entry]);
  return new Map([...groups].map(([id, entries]) => [id, renderSource(entries.sort((a, b) => a.index - b.index))]));
}

function renderSource(entries: PublicationEntry[]): PublicationSource {
  const record = entries[0].record;
  const readable = sourceLabel(record);
  const name = `${readable}-${record.payload.createdAt.slice(0, 10)}-${record.id.slice(0, 12)}.md`;
  const lines = [`# ${record.payload.projectLabel} · ${record.payload.createdAt.slice(0, 10)} 证据`, ""];
  const citations = new Map<string, string[]>();
  for (const entry of entries) {
    const evidence = record.payload.evidence.find(item => item.id === entry.claim.evidenceId)!;
    lines.push(`## ${entry.index + 1}. ${entry.claim.title}`, "");
    const start = lines.length + 1;
    lines.push(...entry.claim.quote.split("\n"));
    const markers = [`^[${name}:${start}${lines.length > start ? `-${lines.length}` : ""}]`];
    lines.push("", `- Locator: ${evidence.locator}`, `- Observed at: ${evidence.observedAt}`,
      `- Evidence kind: ${evidence.kind}`, `- Original evidence SHA-256: ${evidence.originalSha256 ?? evidence.sha256}`, "");
    for (const support of entry.claim.supportingQuotes ?? []) {
      const supportEvidence = record.payload.evidence.find(item => item.id === support.evidenceId);
      if (!supportEvidence) throw new Error("supporting quote evidence is missing");
      lines.push("支持依据：", ...support.quote.split("\n"));
      const supportStart = lines.length - support.quote.split("\n").length + 1;
      markers.push(`^[${name}:${supportStart}${lines.length > supportStart ? `-${lines.length}` : ""}]`);
      lines.push("", `- Locator: ${supportEvidence.locator}`, `- Observed at: ${supportEvidence.observedAt}`,
        `- Evidence kind: ${supportEvidence.kind}`, `- Original evidence SHA-256: ${supportEvidence.originalSha256 ?? supportEvidence.sha256}`, "");
    }
    citations.set(entry.ref, markers);
  }
  const content = lines.join("\n");
  return { name, content, hash: sha256Text(content), citations,
    claimIds: new Map(entries.map(entry => [entry.ref, claimIdentity(record.payload.projectId, entry.claim)])) };
}

function sourceLabel(record: PublicationRecord): string {
  const identity = record.payload.repoIdentity;
  const raw = record.payload.projectLabel === record.payload.projectId && typeof identity === "string" && /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(identity)
    ? identity.split("/").at(-1)! : record.payload.projectLabel;
  return raw.normalize("NFC").replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "").slice(0, 64) || "project";
}

/** String items of a frontmatter list; anything else is ignored. */
export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function segment(entry: PublicationEntry, citation: string): string {
  const { claim, record: { payload } } = entry;
  const status = claim.status === "decided" ? "已确认" : "历史分析／观察";
  return `### ${claim.title}\n\n${status}（${claim.status} · ${claim.kind}），记录于 ${payload.createdAt.slice(0, 10)}\n\n` +
    `${claim.text} ${citation}\n\n适用条件：${claim.useWhen}\n\n依据与取舍：${claim.rationale}\n`;
}

/** Historical analysis cannot silently replace the status of an accepted decision. */
function pageStatus(prior: unknown, statuses: string[]): unknown {
  if (prior && prior !== "historical" && prior !== "decided") return prior;
  return prior === "decided" || statuses.some(status => status === "decided") ? "decided" : "historical";
}

/** Retain original prose and append each publication segment once. */
export function renderTopicPage(page: TopicPage, sources: ReadonlyMap<string, PublicationSource>): string {
  const parsed = parseFrontmatter(page.original ?? "");
  const retained = strings(parsed.meta.knowledgePublicationRefs);
  const fresh = page.entries.filter(entry => !retained.includes(entry.ref));
  if (!fresh.length && page.original) return page.original;
  const first = page.entries[0]; const last = fresh.at(-1)!;
  const meta = topicMetadata(page, sources, parsed.meta, fresh, retained);
  const intro = parsed.body || `## ${page.topic}\n\n决策对象：${page.decisionObject || "沿用原始主题（旧记录未单列对象）"}\n`;
  const body = fresh.map(entry => segment(entry, citationsFor(sources, entry))).join("\n");
  return `${buildFrontmatter({ title: page.topic, summary: first.claim.text.slice(0, 240),
    createdAt: first.record.payload.createdAt, ...meta, updatedAt: last.record.payload.createdAt })}\n\n` +
    `${intro.trimEnd()}\n\n${body}`;
}

function topicMetadata(page: TopicPage, sources: ReadonlyMap<string, PublicationSource>, prior: Record<string, unknown>,
  fresh: PublicationEntry[], retained: string[]): Record<string, unknown> {
  return { ...prior, projectId: page.projectId, projectLabel: page.entries[0].record.payload.projectLabel,
    knowledgeTopic: page.topic, knowledgeDecisionObject: page.decisionObject,
    status: pageStatus(prior.status, fresh.map(entry => entry.claim.status)),
    sources: [...new Set([...strings(prior.sources), ...fresh.map(entry => sources.get(entry.record.id)!.name)])],
    knowledgeClaimIds: [...new Set([...strings(prior.knowledgeClaimIds), ...fresh.map(entry => claimIdentity(page.projectId, entry.claim))])],
    knowledgePublicationRefs: [...retained, ...fresh.map(entry => entry.ref)],
    tags: [...new Set([...strings(prior.tags), page.projectId, page.topic, page.entries[0]?.claim.title ?? "", page.decisionObject].filter(Boolean))] };
}

function citationsFor(sources: ReadonlyMap<string, PublicationSource>, entry: PublicationEntry): string {
  return sources.get(entry.record.id)?.citations.get(entry.ref)?.join(" ") ?? "";
}

/** Render a complete reviewed page replacement while retaining its prior metadata. */
export function renderTopicRevisionPage(revision: TopicRevision, record: PublicationRecord,
  previous: string | undefined, source: PublicationSource, allSources: ReadonlyMap<string, PublicationSource> = new Map()): string {
  const parsed = parseFrontmatter(previous ?? "");
  if (parsed.meta.topicScope === "semantic" && revision.topicScope !== "semantic") throw new Error("legacy topic revision cannot update a semantic page");
  const claimRefs = revision.claimIndexes.map(index => `${record.id}:${index}`);
  const body = revision.body.replace(/\{\{claim:(\d+)\}\}/g, (_match, index: string) => {
    const citation = source.citations.get(`${record.id}:${Number(index)}`)?.join(" ");
    if (!citation) throw new Error("topic revision citation is missing");
    return citation;
  });
  const priorSources = strings(parsed.meta.sources); const priorIds = strings(parsed.meta.knowledgeClaimIds);
  const priorRefs = strings(parsed.meta.knowledgePublicationRefs); const priorTags = strings(parsed.meta.tags);
  // Legacy basis hashes cover these exact serialized bytes, including key order.
  const meta: Record<string, unknown> = { ...parsed.meta, title: revision.title, summary: firstParagraph(body).slice(0, 240),
    projectId: record.payload.projectId, projectLabel: record.payload.projectLabel,
    knowledgeTopicId: revision.topicScope === "semantic" && typeof parsed.meta.knowledgeTopicId === "string"
      ? parsed.meta.knowledgeTopicId : revision.topicId,
    knowledgeTopic: revision.topic,
    knowledgeDecisionObject: revision.decisionObject, status: pageStatus(parsed.meta.status,
      revision.claimIndexes.map(index => record.payload.claims[index].status)),
    createdAt: typeof parsed.meta.createdAt === "string" ? parsed.meta.createdAt : record.payload.createdAt,
    updatedAt: record.payload.createdAt,
    sources: [...new Set([...priorSources, source.name])],
    knowledgeClaimIds: [...new Set([...priorIds, ...revision.claimIndexes.map(index => claimIdentity(record.payload.projectId, record.payload.claims[index]))])],
    knowledgePublicationRefs: [...new Set([...priorRefs, ...claimRefs])],
    tags: [...new Set([...priorTags, record.payload.projectId, revision.topic, revision.title].filter(Boolean))] };
  if (revision.topicScope === "semantic") applySemanticScope(meta, parsed.meta, record);
  return retirePageProvenance(`${buildFrontmatter(meta)}\n\n${body.trimEnd()}\n`, revision.citationRetirements, allSources);
}

function applySemanticScope(meta: Record<string, unknown>, prior: Record<string, unknown>, record: PublicationRecord): void {
  delete meta.projectId; delete meta.projectLabel;
  meta.topicScope = "semantic";
  meta.sourceProjectIds = sourceProjectIds({ ...prior,
    sourceProjectIds: [...sourceProjectIds(prior), record.payload.projectId] });
}

/** The first prose paragraph, used as a page summary. */
export function firstParagraph(body: string): string { return body.split(/\n\s*\n/).find(line => line.trim() && !line.trim().startsWith("#"))?.trim() ?? body.trim(); }
