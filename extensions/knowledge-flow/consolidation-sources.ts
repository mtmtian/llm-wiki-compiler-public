/** Read bounded original sources for prior page citations during whole-page review.
 * Old page prose provides continuity; original cited files let the reviewer check
 * preserved history without treating a session summary as authoritative evidence.
 */
import path from "node:path";
import { readFile } from "node:fs/promises";
import { extractCitations, parseFrontmatter } from "../../src/utils/markdown.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import type { PlannedPage } from "./consolidation-plan.js";

const MAX_PRIOR_SOURCE_CHARS = 200_000;

/** Missing or oversized originals hold an edit instead of silently weakening its review. */
export async function priorSourceContext(wikiRoot: string, pages: PlannedPage[]): Promise<Record<string, string>> {
  const names = [...new Set(pages.flatMap(page => extractCitations(parseFrontmatter(page.original ?? "").body)))].sort();
  return readPriorSources(wikiRoot, names);
}

/** Read the exact source names cited by prior pages with one bounded budget. */
export async function readPriorSources(wikiRoot: string, names: readonly string[]): Promise<Record<string, string>> {
  const result: Record<string, string> = {};
  let total = 0;
  for (const name of [...new Set(names)].sort()) {
    const file = await confineUnderRoot(path.join("sources", name), wikiRoot, { mustExist: true });
    const text = await readFile(file, "utf8");
    total += text.length;
    if (total > MAX_PRIOR_SOURCE_CHARS) throw new Error("prior citation sources exceed whole-page review budget");
    result[name] = text;
  }
  return result;
}
