/** Render complete evidence units and their real reference identities under one hook budget. */
import { createHash } from "node:crypto";
import { extractClaimCitations } from "../../src/utils/markdown.js";
import type { PageTaskEvidence, TaskEvidence } from "../../src/context/task-types.js";

type EvidenceReference = { pageId: string; pageRevision: string; citations: string[] }
  | { claimRef: string; recordRevision: string; citations: string[] };

/** Whole records can be skipped, but never truncated into a different claim or stripped of qualifications. */
export function renderWithinBudget(evidence: TaskEvidence[], seen: Record<string, string>, available: number) {
  let output = "";
  let skipped = false;
  const references: EvidenceReference[] = [];
  const qualified = new Set<string>();
  for (const [index, item] of evidence.entries()) {
    const { id, revision, key } = evidenceIdentity(item);
    const fingerprint = hash(revision + item.text);
    const repeated = index > 0 && seen[key] === fingerprint;
    const text = repeated ? `已提供：${id} · ${item.section}（版本 ${revision.slice(0, 12)}）；需要时补查。\n`
      : renderEvidence(item, !qualified.has(id));
    if (output.length + text.length > available) { skipped = true; continue; }
    output += text;
    if (!repeated) { references.push(reference(item, text)); qualified.add(id); seen[key] = fingerprint; }
  }
  return { output, skipped, references };
}

/** The current projection's content revision refreshes even a stable claim reference after supersession. */
function evidenceIdentity(item: TaskEvidence) {
  return item.origin === "ledger" ? { id: item.claimRef, revision: item.recordRevision, key: `claim:${item.claimRef}` }
    : { id: item.pageId, revision: item.pageRevision, key: `${item.pageId}#${hash(item.section).slice(0, 16)}` };
}

/** Claim references are evidence handles, never page paths or proof of host delivery. */
function reference(item: TaskEvidence, text: string): EvidenceReference {
  return item.origin === "ledger" ? { claimRef: item.claimRef, recordRevision: item.recordRevision, citations: [`[claim:${item.claimRef}]`] }
    : { pageId: item.pageId, pageRevision: item.pageRevision,
      citations: [...new Set(extractClaimCitations(text).map(citation => `^[${citation.raw}]`))] };
}

/** Label observation/recording times explicitly instead of turning them into effective dates. */
function renderEvidence(item: TaskEvidence, includeQualifications: boolean): string {
  if (item.origin !== "ledger") return renderPage(item, includeQualifications);
  const quotes = item.quotes.map(quote => `原始引文（${quote.kind}，观测于 ${quote.observedAt}）：${quote.quote}\n`).join("");
  return `【${item.section} / ${item.title}】\n[claim:${item.claimRef}] · 版本 ${item.recordRevision.slice(0, 12)}\n`
    + `记录时间：${item.updatedAt ?? "未提供"}；条目状态：${item.claimStatus}\n`
    + labels(item) + (item.qualifications ? `适用条件与理由：\n${item.qualifications}\n` : "")
    + `结论：${item.text}\n${quotes}`;
}

/** Keep page applicability once per visible page, retaining existing page citation syntax. */
function renderPage(item: PageTaskEvidence, includeQualifications: boolean): string {
  const scope = includeQualifications && item.qualifications ? `页面适用范围：\n${item.qualifications}\n\n` : "";
  return `【${item.section}】\n页 ${item.pageId} · 版本 ${item.pageRevision.slice(0, 12)}${item.updatedAt ? ` · 页更新 ${item.updatedAt}` : ""}\n`
    + labels(item) + scope + `${item.text}\n`;
}

/** Scope and historical status apply equally to page and ledger origins. */
function labels(item: TaskEvidence): string {
  return (item.decisionObject ? `决策对象：${item.decisionObject}\n` : "")
    + (item.sourceProjectIds?.length ? `来源项目：${item.sourceProjectIds.join("、")}\n` : "")
    + (item.temporalStatus === "historical" ? "时间范围：历史资料，不能据此认定现行规则。\n" : "");
}

function hash(text: string): string { return createHash("sha256").update(text).digest("hex"); }
