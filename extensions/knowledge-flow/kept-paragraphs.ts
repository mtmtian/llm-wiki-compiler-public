/**
 * Kept paragraphs: an update keeps unchanged prose by reference instead of retyping it.
 *
 * Asked to return a whole page, an editor retypes it: old paragraphs get condensed or reworded even
 * where nothing changed, and every citation marker has to be carried along by hand. The editor therefore
 * sees an existing page as numbered paragraphs and may write a paragraph's placeholder (`{{keep:P3}}`)
 * alone on a line to keep it. Before any repair, validation or review, the program puts that paragraph's
 * exact original text back, citation markers included, so reviewers and published revisions never see a
 * placeholder:
 * - a paragraph is a run of non-blank lines, and a fenced code block stays whole across blank lines;
 * - a placeholder alone on its line becomes its paragraph the first time it names one of its page's
 *   paragraphs, set apart from neighbouring text by a blank line so paragraphs never run together;
 *   a repeat, or a number past the page's last paragraph, keeps nothing and is dropped;
 * - a placeholder inside other text, or in a new page's body, stays literal and validation rejects it,
 *   so a misuse becomes ordinary correction feedback instead of silently misplaced text. As with
 *   `{{claim:N}}`, page prose therefore cannot contain the literal placeholder syntax.
 */
import { parseFrontmatter } from "../../src/utils/markdown.js";
import type { TopicDraft } from "./consolidation-draft.js";
import type { PlannedPage } from "./consolidation-plan.js";

const FENCE = /^\s*(`{3,}|~{3,})/;
const KEEP_LINE = /^\s*\{\{keep:P(\d+)\}\}\s*$/;
const KEEP_PLACEHOLDER = /\{\{keep:[^}\r\n]*\}\}/;

/** The editor's view of planned pages: an existing body arrives as paragraphs it can keep by placeholder. */
export function editablePages(pages: readonly PlannedPage[]) {
  return pages.map(({ original, ...page }) => original === null ? { ...page, original }
    : { ...page, originalParagraphs: pageParagraphs(original).map((text, index) => ({ keep: `{{keep:P${index + 1}}}`, text })) });
}

/** Put the exact original paragraph back for every valid placeholder of each drafted page. */
export function withKeptParagraphs(draft: TopicDraft, pages: readonly PlannedPage[]): TopicDraft {
  return { ...draft, pages: draft.pages.map(edit => {
    const original = pages.find(page => page.pageId === edit.pageId)?.original;
    return original ? { ...edit, body: expandKept(edit.body, pageParagraphs(original)) } : edit;
  }) };
}

/** The first placeholder that expansion left in a body: one inside other text, or on a new page. */
export function unexpandedPlaceholder(body: string): string | undefined {
  return KEEP_PLACEHOLDER.exec(body)?.[0];
}

/** Split a page body into paragraphs: runs of non-blank lines, with fenced code blocks kept whole. */
export function pageParagraphs(original: string): string[] {
  const paragraphs: string[] = [];
  let current: string[] = [];
  let fence: string | null = null;
  for (const line of parseFrontmatter(original).body.split("\n")) {
    const marker = FENCE.exec(line)?.[1];
    if (fence === null && !marker && line.trim() === "") {
      if (current.length) paragraphs.push(current.join("\n"));
      current = [];
      continue;
    }
    current.push(line);
    if (fence === null) fence = marker ?? null;
    else if (marker && closesFence(marker, fence)) fence = null;
  }
  if (current.length) paragraphs.push(current.join("\n"));
  return paragraphs;
}

/** A closing fence repeats the opening character at least as many times (CommonMark). */
function closesFence(marker: string, opening: string): boolean {
  return marker[0] === opening[0] && marker.length >= opening.length;
}

interface Block { text: string; kept: boolean }

/** Replace each standalone placeholder by its paragraph; one naming no new paragraph is dropped. */
function expandKept(body: string, paragraphs: readonly string[]): string {
  const used = new Set<number>();
  return joinSeparated(body.split("\n").flatMap((line): Block[] => {
    const id = KEEP_LINE.exec(line)?.[1];
    if (id === undefined) return [{ text: line, kept: false }];
    const index = Number(id) - 1;
    if (used.has(index) || paragraphs[index] === undefined) return [];
    used.add(index);
    return [{ text: paragraphs[index], kept: true }];
  }));
}

/** Join blocks by line, with a blank line wherever a kept paragraph meets adjacent non-blank text. */
function joinSeparated(blocks: readonly Block[]): string {
  return blocks.map((block, index) => {
    const previous = blocks[index - 1];
    const adjacent = previous !== undefined && (block.kept || previous.kept);
    return adjacent && block.text.trim() && previous.text.trim() ? `\n${block.text}` : block.text;
  }).join("\n");
}
