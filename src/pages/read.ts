/**
 * Page-reading utilities for llmwiki.
 *
 * Exposes `readPageRecord`, which locates a wiki page by slug across the
 * priority-ordered page directories (concepts first, then queries), parses
 * its frontmatter, and returns a structured `PageRecord`. Orphaned pages are
 * silently skipped to match the query pipeline's behaviour.
 *
 * This module is shared between the MCP tool layer and the in-process SDK so
 * both consumers work from identical read semantics.
 * Slugs are untrusted input; reads accept one filename component and confine
 * the resolved file to its page directory, rejecting traversal and escaping symlinks.
 */

import { parseFrontmatter } from "../utils/markdown.js";
import { CONCEPTS_DIR, QUERIES_DIR } from "../utils/constants.js";
import { readConfinedWikiPage } from "../compiler/confined-wiki-read.js";

/** Directories searched (in priority order) when resolving a page slug. */
const PAGE_DIRS = [CONCEPTS_DIR, QUERIES_DIR];

/** Shape returned by readPageRecord and search_pages for each matching page. */
export interface PageRecord {
  slug: string;
  title: string;
  summary: string;
  body: string;
}

/**
 * Read a page only when `slug` names one file directly inside `dir`.
 * Unicode, spaces, `#`, and `%` are valid filename characters; separators and
 * NUL are rejected before the confined reader checks the resolved path.
 *
 * @param root - Absolute path to the wiki workspace root.
 * @param dir - Project-relative page directory, such as `wiki/concepts`.
 * @param slug - Untrusted page slug without the `.md` extension.
 * @returns Page content, or `null` when absent or outside the directory.
 */
export async function readPageContent(root: string, dir: string, slug: string): Promise<string | null> {
  if (!slug || /[/\\\0]/.test(slug)) return null;
  const result = await readConfinedWikiPage(root, dir, slug);
  return "content" in result ? result.content : null;
}

/**
 * Locate a page by slug across the priority-ordered page directories,
 * skipping orphaned entries to match the query pipeline's behaviour.
 *
 * @param root - Absolute path to the wiki workspace root.
 * @param slug - Page slug without the `.md` extension.
 * @returns The parsed page record, or `null` if not found or orphaned.
 */
export async function readPageRecord(root: string, slug: string): Promise<PageRecord | null> {
  for (const dir of PAGE_DIRS) {
    const content = await readPageContent(root, dir, slug);
    if (!content) continue;

    const { meta, body } = parseFrontmatter(content);
    if (meta.orphaned) continue;

    return {
      slug,
      title: typeof meta.title === "string" ? meta.title : slug,
      summary: typeof meta.summary === "string" ? meta.summary : "",
      body: body.trim(),
    };
  }
  return null;
}
