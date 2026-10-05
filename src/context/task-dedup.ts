/**
 * Deduplicate complete page/ledger representations only when reviewed record identity and
 * the selected paragraph agree. Partial overlap and missing qualifications remain separate.
 */
import type { ReviewedClaim } from "./ledger.js";
import type { TaskSelection } from "./task-claims.js";
import type { DecisionSection } from "./task-sections.js";
import type { TaskEvidence } from "./task-types.js";

export interface SelectedEvidence { selection: TaskSelection; evidence: TaskEvidence }

/** A missing lineage or an additional paragraph is not evidence of complete equivalence. */
export function duplicateSelection(left: TaskSelection, right: TaskSelection): boolean {
  if (left.origin === "page" && right.origin === "ledger") return representsOnlyClaim(left.section, right.claim);
  return left.origin === "ledger" && right.origin === "page" && representsOnlyClaim(right.section, left.claim);
}

/** Resolve the page's citations before treating the sealed record link as a budget-saving duplicate. */
export function duplicateEvidence(left: SelectedEvidence, right: SelectedEvidence): boolean {
  if (!duplicateSelection(left.selection, right.selection)) return false;
  const ledger = left.evidence.origin === "ledger" ? left.evidence : right.evidence;
  const page = left.evidence.origin === "ledger" ? right.evidence : left.evidence;
  return ledger.origin === "ledger" && page.origin !== "ledger"
    && ledger.quotes.every(quote => page.sources.some(source => source.text === quote.quote));
}

/** Exact paragraphs preserve negations; only the known publication status wrapper is non-knowledge text. */
function representsOnlyClaim(section: DecisionSection, claim: ReviewedClaim): boolean {
  const refs = section.page.frontmatter.knowledgePublicationRefs;
  if (!Array.isArray(refs) || !claim.equivalentPageRefs.some(ref => refs.includes(ref))) return false;
  const paragraphs = (section.text + "\n\n" + (section.qualifications ?? "")).split(/\n\s*\n/).map(plain).filter(Boolean);
  const required = [claim.text, `适用条件：${claim.useWhen}`, `依据与取舍：${claim.rationale}`].map(plain);
  const allowed = new Set([...required, plain(`决策对象：${claim.decisionObject}`)]);
  const status = plain(`${claim.status === "decided" ? "已确认" : "历史分析／观察"}（${claim.status} · ${claim.kind}），记录于 `);
  return required.every(value => paragraphs.includes(value)) && paragraphs.every(value => allowed.has(value)
    || (value.startsWith(status) && /^\d{4}-\d{2}-\d{2}$/.test(value.slice(status.length))));
}

/** Keep actual statement characters intact while ignoring citations and layout whitespace. */
function plain(value: string): string { return value.replace(/\^\[[^\]]*\]/g, "").replace(/\s+/g, "").normalize("NFC"); }
