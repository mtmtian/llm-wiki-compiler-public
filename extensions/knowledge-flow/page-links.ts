/**
 * Wiki-link rewriting for pages that moved: a reviewed migration or merge removes pages, and every
 * remaining `[[concepts/old]]`, `[[old]]` or `[[old|label]]` link must point at the surviving page.
 */

/** Rewrite links in every page of `pages` in place. */
export function rewritePageLinks(pages: Map<string, string>, links: ReadonlyMap<string, string>): void {
  for (const [id, body] of pages) pages.set(id, rewriteBody(body, links));
}

/** Rewrite links in one page body. */
export function rewriteBody(body: string, links: ReadonlyMap<string, string>): string {
  let result = body;
  for (const [previous, target] of links) {
    const oldSlug = previous.slice("concepts/".length); const newSlug = target.slice("concepts/".length);
    for (const [from, to] of [[previous, target], [oldSlug, newSlug]]) {
      result = result.replaceAll(`[[${from}]]`, `[[${to}]]`).replaceAll(`[[${from}|`, `[[${to}|`);
    }
  }
  return result;
}
