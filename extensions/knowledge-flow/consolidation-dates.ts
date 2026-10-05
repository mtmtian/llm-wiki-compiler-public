/** Reject high-confidence report or effective dates copied from capture or page-publication metadata. */
import type { TopicDraft } from "./consolidation-draft.js";
import type { PlannedPage } from "./consolidation-plan.js";
import { pageParagraphs, pagePublishedAt } from "./kept-paragraphs.js";
import type { FlowClaim, FlowEvidence } from "./types.js";

const FULL_DATE = /(?<!\d)(\d{4}(?:-\d{2}-\d{2}|\/\d{1,2}\/\d{1,2}|年\d{1,2}月\d{1,2}日))(?!\d)/gu;
const CLAIM_MARKER = /\{\{claim:(\d+)\}\}/g;
const HEADING = /^ {0,3}#{1,6}(?:[\t ]|$)/;
const BAD_DATE_BEFORE = /(?:报告(?:日期)?(?:是|为|在|于|[:：（(])?|报道(?:日期)?(?:是|为|在|于|[:：（(])?|reported\s+(?:on|at)|report\s+date\s+(?:is|was)|生效(?:日期)?(?:是|为|在|于|[:：])?|实施(?:日期)?(?:是|为|在|于|[:：])?|took\s+effect\s+(?:on|from)|effective(?:\s+date)?\s+(?:on|from))\s*$/iu;
const BAD_DATE_AFTER = /^\s*(?:(?:was|is|were)\s+)?(?:reported|effective|implemented)\b|^\s*(?:报告|报道|生效|实施|起效)/iu;
const DATED_REPORT = /^\s*(?:\d{1,2}:\d{2}(?::\d{2})?\s*)?的\s*([^，。；：！？\r\n.!?;:]{0,16}?)(?:报告|报道)/u;
const CAPTURE_QUALIFIER = /捕获|采集|captur(?:e|ed)/iu;
const DATE_PREFIX_CONTEXT = 64;
const DATE_SUFFIX_CONTEXT = 40;

interface DateOccurrence { value: string; start: number; end: number }

/** Fail when a new claim-linked sentence turns capture or publication metadata into an event date. */
export function assertEvidenceDates(draft: TopicDraft, pages: readonly PlannedPage[], evidence: readonly FlowEvidence[]): void {
  const evidenceById = new Map(evidence.map(item => [item.id, item]));
  for (const edit of draft.pages) {
    const page = pages.find(item => item.pageId === edit.pageId);
    if (page) assertPageDates(edit, page, draft.claims, evidenceById);
  }
}

/** Match only claim-linked changes within one page and its heading context. */
function assertPageDates(edit: TopicDraft["pages"][number], page: PlannedPage, claims: readonly FlowClaim[],
  evidence: ReadonlyMap<string, FlowEvidence>): void {
  const priorParagraphs = page.original === null ? [] : pageParagraphs(page.original);
  const publishedDates = new Set(dateKeys(pagePublishedAt(page.original)));
  let sectionHeading = "";
  for (const paragraph of pageParagraphs(edit.body)) {
    const headings = paragraph.split("\n").filter(line => HEADING.test(line));
    if (headings.length) sectionHeading = headings.join("\n");
    const hasChangedProse = !isUnchanged(paragraph, priorParagraphs);
    for (const claimIndex of claimIndexes(paragraph, edit.claimIndexes)) {
      const claim = claims[claimIndex];
      if (!claim) continue;
      const contexts = hasChangedProse ? [paragraph, sectionHeading, claim.text,
        `${sectionHeading}\n${paragraph}`, `${sectionHeading}\n${claim.text}`] : [claim.text];
      assertClaimDate(contexts, claimIndex, claim, evidence, publishedDates);
    }
  }
}

/** A date stated by this claim's quotes is left to semantic review; metadata alone cannot supply it. */
function assertClaimDate(contexts: readonly string[], claimIndex: number, claim: FlowClaim,
  evidenceById: ReadonlyMap<string, FlowEvidence>, publishedDates: ReadonlySet<string>): void {
  const sources = [claim.quote, ...(claim.supportingQuotes ?? []).map(item => item.quote)];
  const quotedDates = new Set(sources.flatMap(dateKeys));
  const metadataDates = new Set([...publishedDates, ...linkedEvidence(claim, evidenceById)
    .flatMap(item => dateKeys(item.observedAt))]);
  for (const context of contexts) {
    for (const occurrence of dateOccurrences(context)) {
      if (!metadataDates.has(occurrence.value) || quotedDates.has(occurrence.value)) continue;
      if (hasMisattributionLabel(context, occurrence)) {
        throw new Error(`claim ${claimIndex} attributes capture or publication metadata date ${occurrence.value} as a report or effective date`);
      }
    }
  }
}

/** A different claim's capture timestamp must not influence this claim's date check. */
function linkedEvidence(claim: FlowClaim, evidenceById: ReadonlyMap<string, FlowEvidence>): FlowEvidence[] {
  const ids = [claim.evidenceId, ...(claim.supportingQuotes ?? []).map(item => item.evidenceId)];
  return [...new Set(ids)].flatMap(id => {
    const item = evidenceById.get(id);
    return item ? [item] : [];
  });
}

/** Only declared claim markers connect a paragraph to newly selected evidence. */
function claimIndexes(paragraph: string, allowed: readonly number[]): number[] {
  const indexes = new Set<number>();
  for (const match of paragraph.matchAll(CLAIM_MARKER)) {
    const index = Number(match[1]);
    if (allowed.includes(index)) indexes.add(index);
  }
  return [...indexes];
}

/** Reattaching a new claim marker does not make otherwise identical old prose a required repair. */
function isUnchanged(paragraph: string, priorParagraphs: readonly string[]): boolean {
  const normalize = (text: string) => text.replace(/\{\{claim:\d+\}\}/g, "").trim();
  const current = normalize(paragraph);
  return priorParagraphs.some(previous => normalize(previous) === current);
}

/** Compare calendar dates across supported source and metadata formats. */
function dateKeys(text: string | null): string[] {
  return text === null ? [] : dateOccurrences(text).map(item => item.value);
}

/** Preserve string offsets so attribution labels stay local to each occurrence. */
function dateOccurrences(text: string): DateOccurrence[] {
  const occurrences: DateOccurrence[] = [];
  for (const match of text.matchAll(FULL_DATE)) {
    const value = canonicalDate(match[1]);
    if (value) occurrences.push({ value, start: match.index ?? 0, end: (match.index ?? 0) + match[0].length });
  }
  return occurrences;
}

/** Ignore malformed dates instead of normalizing an impossible calendar day into another date. */
function canonicalDate(value: string): string | undefined {
  const match = /^(\d{4})(?:-(\d{2})-(\d{2})|\/(\d{1,2})\/(\d{1,2})|年(\d{1,2})月(\d{1,2})日)$/.exec(value);
  if (!match) return undefined;
  const year = Number(match[1]);
  const month = Number(match[2] ?? match[4] ?? match[6]);
  const day = Number(match[3] ?? match[5] ?? match[7]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return undefined;
  return `${match[1]}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** Limit deterministic rejection to explicit adjacent report or effective-date wording. */
function hasMisattributionLabel(text: string, date: DateOccurrence): boolean {
  const before = text.slice(Math.max(0, date.start - DATE_PREFIX_CONTEXT), date.start);
  const after = text.slice(date.end, date.end + DATE_SUFFIX_CONTEXT);
  const report = DATED_REPORT.exec(after);
  return BAD_DATE_BEFORE.test(before) || BAD_DATE_AFTER.test(after)
    || (report !== null && !CAPTURE_QUALIFIER.test(report[1]));
}
