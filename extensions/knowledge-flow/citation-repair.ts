/**
 * Deterministic repair of drafted page citations, applied before validation and independent review.
 *
 * Most citation holds come from a draft that rewrote a large page and lost markers the page already
 * had. The repair only restores provenance the draft visibly kept, and never decides what to keep:
 * - a claim placeholder wrapped in marker syntax (`^[{{claim:0}}]`) becomes the plain placeholder;
 * - a new single-source marker that renumbers or merges dropped original ranges of the same file is
 *   replaced by those original markers;
 * - a line kept verbatim (ignoring markers and whitespace) that lost its markers gets the original
 *   line back, as long as it carries no marker the original line did not have.
 * Markers declared in citationRetirements are never restored. Anything the repair cannot prove still
 * reaches the strict validator (citation-retirement.ts) unchanged, and the reviewer sees the result.
 */
import { parseFrontmatter } from "../../src/utils/markdown.js";
import { citationMarkers } from "./citation-retirement.js";
import type { TopicDraft } from "./consolidation-draft.js";
import type { PlannedPage } from "./consolidation-plan.js";

const MARKER = /\^\[[^\]\r\n]+\]/g;
const SINGLE_SOURCE = /^\^\[([^\]:,\r\n]+):(\d+)(?:-(\d+))?\]$/;
const WRAPPED_PLACEHOLDER = /\^\[(\{\{claim:\d+\}\})\]/g;
// Shorter lines (headings, bullets, fragments) are too ambiguous to identify as kept verbatim.
const MIN_RESTORED_LINE = 8;

/** Repair every planned page of a draft against its frozen original. */
export function withRepairedCitations(draft: TopicDraft, pages: readonly PlannedPage[]): TopicDraft {
  return { ...draft, pages: draft.pages.map(edit => {
    const original = pages.find(page => page.pageId === edit.pageId)?.original;
    const retired = (edit.citationRetirements ?? []).map(item => item.citation);
    return { ...edit, body: repairCitations(edit.body, original, retired) };
  }) };
}

/** Repair one drafted body; a new page (no original) only gets wrapped placeholders unwrapped. */
export function repairCitations(body: string, original: string | null | undefined, retired: readonly string[]): string {
  const unwrapped = body.replace(WRAPPED_PLACEHOLDER, "$1");
  if (!original) return unwrapped;
  const before = parseFrontmatter(original).body;
  const skip = new Set(retired);
  return restoreLines(restoreRanges(unwrapped, before, skip), before, skip);
}

/** The citations each existing page already has: the draft must keep or retire every one. */
export function citationChecklist(pages: readonly PlannedPage[]): Array<{ pageId: string; existingCitations: string[] }> {
  return pages.flatMap(page => page.original ? [{ pageId: page.pageId, existingCitations: citationMarkers(page.original) }] : []);
}

/**
 * Correction feedback per page: original markers the previous draft neither kept nor retired, markers it
 * invented, and retirements of markers the page never had. Empty kinds are omitted.
 */
export function unaccountedCitations(previous: TopicDraft, pages: readonly PlannedPage[]):
  Array<{ pageId: string; citations: string[]; invented?: string[]; outsideBasis?: string[] }> {
  return pages.flatMap(page => {
    const edit = previous.pages.find(item => item.pageId === page.pageId);
    if (!edit) return [];
    const before = new Set(page.original ? citationMarkers(page.original) : []);
    const drafted = citationMarkers(edit.body);
    const retired = (edit.citationRetirements ?? []).map(item => item.citation);
    const citations = [...before].filter(item => !drafted.includes(item) && !retired.includes(item));
    const invented = drafted.filter(item => !before.has(item));
    const outsideBasis = retired.filter(item => !before.has(item));
    if (!citations.length && !invented.length && !outsideBasis.length) return [];
    return [{ pageId: page.pageId, citations, ...(invented.length ? { invented } : {}),
      ...(outsideBasis.length ? { outsideBasis } : {}) }];
  });
}

function markers(text: string): string[] {
  return [...new Set(text.match(MARKER) ?? [])];
}

function range(marker: string): { file: string; start: number; end: number } | null {
  const match = SINGLE_SOURCE.exec(marker);
  return match ? { file: match[1], start: Number(match[2]), end: Number(match[3] ?? match[2]) } : null;
}

/** Swap a renumbered or merged marker back to the dropped original markers it covers. */
function restoreRanges(body: string, before: string, retired: ReadonlySet<string>): string {
  const originals = markers(before);
  let result = body;
  for (const marker of markers(body).filter(item => !originals.includes(item))) {
    const drafted = range(marker);
    const present = new Set(markers(result));
    const covered = drafted ? originals.filter(item => {
      const old = range(item);
      return old !== null && old.file === drafted.file && old.start <= drafted.end && drafted.start <= old.end
        && !present.has(item) && !retired.has(item);
    }) : [];
    if (covered.length) result = result.split(marker).join(covered.join(""));
  }
  return result;
}

/** Give a line kept verbatim back the markers the original line carried. */
function restoreLines(body: string, before: string, retired: ReadonlySet<string>): string {
  const originals = new Map<string, string | null>();
  for (const line of before.split("\n")) {
    const cited = markers(line);
    const key = plain(line);
    if (!cited.length || cited.some(item => retired.has(item)) || key.length < MIN_RESTORED_LINE) continue;
    originals.set(key, originals.has(key) ? null : line); // duplicated lines are ambiguous: leave them alone
  }
  return body.split("\n").map(line => {
    const restored = originals.get(plain(line));
    if (!restored || restored === line) return line;
    const kept = markers(line);
    return kept.every(item => restored.includes(item)) && markers(restored).some(item => !kept.includes(item)) ? restored : line;
  }).join("\n");
}

function plain(line: string): string {
  return line.replace(MARKER, "").replace(/\s+/g, " ").trim();
}
