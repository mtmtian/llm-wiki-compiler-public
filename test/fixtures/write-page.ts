/**
 * Shared test helper for writing wiki pages with frontmatter.
 * Reduces duplication across test files that need to create wiki pages.
 */

import { writeFile } from "fs/promises";
import path from "path";
import { buildFrontmatter } from "../../src/utils/markdown.js";

/**
 * Write a wiki page with frontmatter to a directory.
 * @param dir - Absolute path to the target directory (e.g. wiki/concepts).
 * @param slug - Filename without extension.
 * @param fields - Frontmatter fields to include.
 * @param body - Markdown body content.
 */
export async function writePage(
  dir: string,
  slug: string,
  fields: Record<string, unknown>,
  body: string,
): Promise<void> {
  const fm = buildFrontmatter(fields);
  await writeFile(path.join(dir, `${slug}.md`), `${fm}\n\n${body}\n`);
}
