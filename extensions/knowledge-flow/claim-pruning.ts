/**
 * Partial page publication: when a final review rejects some claims, publish the accepted ones alone.
 *
 * A batch used to be held whenever its final review rejected any claim, even when it accepted most of
 * them, because page revisions publish as a whole. Drafts place each new claim's `{{claim:N}}`
 * placeholder on its own line, apart from lines that keep existing `^[...]` citations, so the lines
 * carrying a rejected claim can be removed without touching anything already published. The result is
 * only a candidate: the caller validates it again and asks for a fresh independent review.
 *
 * Nothing is pruned when the review's per-claim conclusions are incomplete, when no claim (or every
 * claim) was rejected, or when a rejected claim shares a line with an accepted claim or an existing
 * citation. A page whose claims were all rejected keeps its current text.
 */
import type { ClaimReview } from "./types.js";
import type { TopicDraft } from "./consolidation-draft.js";

const PLACEHOLDER = /\{\{claim:(\d+)\}\}/g;
const EXISTING_CITATION = "^[";

/** The draft restricted to the claims a complete review accepted; null when that cannot be done safely. */
export function withoutRejectedClaims(draft: TopicDraft, review: ClaimReview | undefined): TopicDraft | null {
  if (!review?.complete) return null;
  const rejected = new Set(review.claims.filter(item => item.decision !== "accept").map(item => item.claimIndex));
  if (!rejected.size || rejected.size >= draft.claims.length) return null;
  const kept = draft.claims.flatMap((_, index) => rejected.has(index) ? [] : [index]);
  const renumber = new Map(kept.map((previous, index) => [previous, index]));
  const pages: TopicDraft["pages"] = [];
  for (const page of draft.pages) {
    const claimIndexes = page.claimIndexes.filter(index => !rejected.has(index));
    if (!claimIndexes.length) continue;
    const body = withoutRejectedLines(page.body, rejected);
    if (body === null) return null;
    pages.push({ ...page, body: renumbered(body, renumber), claimIndexes: claimIndexes.map(index => renumber.get(index)!) });
  }
  return { ...draft, claims: kept.map(index => draft.claims[index]), pages };
}

/** Remove every line citing a rejected claim; null when such a line also carries anything that must stay. */
function withoutRejectedLines(body: string, rejected: ReadonlySet<number>): string | null {
  const lines: Array<string | null> = [];
  for (const line of body.split("\n")) {
    const cited = [...line.matchAll(PLACEHOLDER)].map(match => Number(match[1]));
    if (!cited.some(index => rejected.has(index))) { lines.push(line); continue; }
    if (line.includes(EXISTING_CITATION) || !cited.every(index => rejected.has(index))) return null;
    lines.push(null);
  }
  withoutEmptiedSections(lines);
  return lines.filter((line): line is string => line !== null).join("\n").replace(/\n{3,}/g, "\n\n");
}

/**
 * Remove (set to null) each heading whose section lost lines and has no content left. Headings are visited
 * from the end, so a parent whose only content was an emptied subsection goes too; sections that were
 * already empty are left as they were.
 */
function withoutEmptiedSections(lines: Array<string | null>): void {
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const level = headingLevel(lines[index]);
    if (!level) continue;
    let end = index + 1;
    while (end < lines.length && !(headingLevel(lines[end]) && headingLevel(lines[end]) <= level)) end += 1;
    const section = lines.slice(index + 1, end);
    if (section.includes(null) && section.every(line => line === null || !line.trim())) lines[index] = null;
  }
}

function headingLevel(line: string | null): number {
  return line === null ? 0 : /^(#{1,6})\s/.exec(line)?.[1].length ?? 0;
}

function renumbered(body: string, renumber: ReadonlyMap<number, number>): string {
  return body.replace(PLACEHOLDER, (match, index: string) => {
    const next = renumber.get(Number(index));
    return next === undefined ? match : `{{claim:${next}}}`;
  });
}
