/**
 * llmwiki viewer — what the Overview dashboard calls its page inventory.
 *
 * The dashboard used to read `counts.concepts` for its first stat card, its
 * hero sentence, and its "All N concepts →" link. On a profile project that
 * count is 0 — the pages are in the profile's own typed directories — so a
 * newsroom with twelve typed pages reported "Concepts 0" beside a sidebar
 * correctly reading Articles 6 / Desks 3 / Bylines 3. The Overview contradicted
 * the sidebar on the same screen.
 *
 * Everything that varies with the active profile's vocabulary is decided here,
 * governed by two rules:
 *
 *   1. NO LABEL MAY MISDESCRIBE ITS NUMBER. A card headed "Concepts" showing a
 *      count of articles is the same defect the sidebar had. Where the label
 *      cannot be true, the label changes — not just the number.
 *   2. ABSENCE MEANS UNCHANGED. A default project's envelope carries no
 *      `profilePipeline`, so {@link profileVocabulary} returns null and every
 *      function below hands back exactly what it handed back before this module
 *      existed. That is the half with no room for interpretation.
 *
 * The card SET stays four. A profile may declare a dozen entity types, so the
 * per-type detail belongs to the sidebar and `#/pipeline`, not to a grid that
 * would grow with the vocabulary.
 *
 * The page total is summed from `profilePipeline.entityTypes[].pageCount` — the
 * very figures the sidebar's type rows show — so the two surfaces agree by
 * construction rather than by two derivations happening to match.
 */

import { plural } from "./viewer-format.js";

/** Where a profile project's "all of it" links go: one row per declared type. */
const PIPELINE_HREF = "#/pipeline";

/** Where a default project's do: the list route its concept pages live on. */
const CONCEPTS_HREF = "#/concepts";

/**
 * The active profile's page inventory, or null on a default project.
 *
 * Null is the whole default-parity guarantee: every consumer below branches on
 * it, and the null branch is today's code verbatim.
 *
 * @param {object} envelope - The `/api/pages` envelope.
 * @param {{pageDirectory?: string, citationCount?: number}[]} pages - Its `pages` rows.
 * @returns {{typeCount: number, pageCount: number, citationCount: number}|null}
 */
export function profileVocabulary(envelope, pages) {
  const entityTypes = envelope?.profilePipeline?.entityTypes;
  if (!Array.isArray(entityTypes)) return null;
  const declared = new Set(entityTypes.map((entry) => entry?.type));
  return {
    typeCount: entityTypes.length,
    pageCount: sumBy(entityTypes, (entry) => countOf(entry?.pageCount)),
    citationCount: sumBy(
      pages.filter((page) => declared.has(page.pageDirectory)),
      (page) => countOf(page.citationCount),
    ),
  };
}

/** Sum a numeric projection over a list. */
function sumBy(items, project) {
  return items.reduce((total, item) => total + project(item), 0);
}

/** A count as a number, with anything non-numeric read as none. */
function countOf(value) {
  return typeof value === "number" && value > 0 ? value : 0;
}

/**
 * The first stat card's descriptor: the wiki's inventory, in this project's own
 * terms.
 *
 * A profile project's card is keyed `pages` rather than `concepts` so the key
 * describes its contents too, and its sub-line reports how many TYPES those
 * pages span — the fact a default project has no equivalent of, and the one
 * that says why the number is bigger than any single sidebar row.
 *
 * @param {ReturnType<typeof profileVocabulary>} vocabulary
 * @returns {object} A card descriptor for `buildStatCard` (viewer-stat-card.js).
 */
export function inventoryCard(vocabulary) {
  if (!vocabulary) return CONCEPTS_CARD;
  return {
    key: "pages",
    label: "Entity pages",
    badge: "PAGES",
    value: (m) => m.vocabulary.pageCount,
    sub: (m) =>
      `${plural(m.vocabulary.citationCount, "citation")} · ${plural(m.vocabulary.typeCount, "type")}`,
  };
}

/**
 * The default profile's own inventory card, unchanged.
 *
 * Its sub-line is scoped to concept pages (not the envelope-wide
 * `totalCitations`, which also counts queries) — the card is named "Concepts",
 * so it must describe concepts.
 */
const CONCEPTS_CARD = {
  key: "concepts",
  label: "Concepts",
  badge: "PAGES",
  value: (m) => m.counts.concepts ?? 0,
  sub: (m) => `${plural(m.conceptsCitations, "citation")} · ${plural(m.counts.concepts ?? 0, "page")}`,
};

/**
 * The hero's "N pages, M citations" figures.
 *
 * A profile project counts the pages its profile declares and the citations
 * those pages carry, so the two halves of the sentence describe one set of
 * pages rather than two.
 *
 * @param {object} model - The dashboard model.
 * @returns {{pages: number, citations: number}}
 */
export function heroTotals(model) {
  if (!model.vocabulary) return { pages: model.counts.concepts ?? 0, citations: model.totalCitations };
  return { pages: model.vocabulary.pageCount, citations: model.vocabulary.citationCount };
}

/**
 * Where the recently-compiled panel's two links point, and what the footer one
 * says.
 *
 * "All 12 pages" would name a destination that does not exist — no route lists
 * every typed page across every type. `#/pipeline` lists every TYPE, so that is
 * what the link counts and what it says: number, noun and destination all true.
 *
 * @param {object} model - The dashboard model.
 * @returns {{href: string, allText: string}}
 */
export function inventoryLink(model) {
  const vocabulary = model.vocabulary;
  if (!vocabulary) {
    return {
      href: CONCEPTS_HREF,
      allText: `All ${plural(model.counts.concepts ?? 0, "concept")} →`,
    };
  }
  return { href: PIPELINE_HREF, allText: `All ${plural(vocabulary.typeCount, "type")} →` };
}
