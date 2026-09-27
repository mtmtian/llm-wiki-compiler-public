/**
 * llmwiki viewer — the health screen's Lint panel.
 *
 * The largest object on `#/health`: a head chipped with the combined problem
 * count, the error/warning figures beside a stacked proportion bar and its
 * legend, a row-per-rule table, and a footer naming the rule with the most
 * leverage. Everything here is projected from `lint.rules[]` — the per-rule
 * breakdown the linter persists into `.llmwiki/last-lint.json` — so the
 * whole panel degrades to the "run `llmwiki lint`" placeholder when that
 * cache has never been written (`lint === null` is a real, common state, not
 * an error).
 *
 * Split out of viewer-health.js rather than folded into it: the two together
 * would push a single module past the project's 400-line file budget
 * (CLAUDE.md), and the panel is a self-contained view over one payload
 * field.
 *
 * Read-only viewer: the mockup's per-row fix verbs ("create →", "map →") and
 * its "Resolve dangling links" button are mutations this snapshot has no
 * write path for. The FIX column navigates instead — to the most-affected
 * page when that file is one the viewer can route to — and the footer button
 * navigates to whichever destination the dominant rule actually implies (see
 * {@link footerAction}).
 */

import { el, placeholder } from "./viewer-dom.js";
import { lintTotal, plural } from "./viewer-format.js";

/** Rendered in the FIX column when a rule's file is not a routable page. */
const NO_ROUTE = "—";

/**
 * How many rules get a colour of their own. The palette has exactly four
 * entries (danger, warn, accent, ok — viewer-health.css `[data-rank]`), and
 * the bar segment, its legend swatch and the table's rule name all read from
 * the same rank, so a fifth colour is not available to invent. Sixteen lint
 * rules can fire, so everything past the fourth folds into {@link OTHER_RULE}
 * rather than wrapping onto a colour a named rule already owns.
 */
const NAMED_RULE_LIMIT = 4;

/**
 * The row standing for every rule below the palette. Named "other" rather
 * than "other rules" so the single-rule case — five rules firing, the common
 * one — reads correctly; the MOST AFFECTED cell beside it carries the count.
 */
const OTHER_RULE = "other";

/**
 * Rules whose findings are facts about the wikilink graph: a link with no
 * target, a page with no live sources behind it, a page carrying fewer
 * cross-links than its kind requires. The graph explorer draws exactly that
 * structure — dangling targets render there as ghost nodes — so it is the
 * destination that matches. `pending-target` is deliberately absent: it is an
 * info-severity rule, and `aggregateRules` never persists info findings, so
 * it can never be the dominant rule here.
 */
const LINK_GRAPH_RULES = new Set([
  "broken-wikilink",
  "orphaned-page",
  "schema-cross-link-minimum",
]);

/** Path shape of a file the viewer can route to: `wiki/<dir>/<slug>.md`. */
const PAGE_FILE_PATTERN = /^wiki\/(concepts|queries)\/([^/]+)\.md$/;

/**
 * Build the Lint panel from the `/api/health` lint cache.
 *
 * @param {object|null} lint - The lint cache entry, or null when lint has
 *   never run.
 * @param {object} pageIndex - Routable pages, keyed `"<directory>/<slug>"`
 *   (see `routablePageIndex` in viewer-health.js) — used to decide whether a
 *   rule's most-affected file has a page route to link to.
 * @returns {HTMLElement}
 */
export function buildLintPanel(lint, pageIndex) {
  const total = lintTotal(lint);
  const panel = el("section", lintPanelClass(total));
  panel.dataset.lintPanel = "";
  panel.appendChild(buildLintHead(total));
  if (total === null) {
    panel.appendChild(placeholder("No cached lint summary yet — run `llmwiki lint`."));
    return panel;
  }
  const rules = rulesOf(lint);
  const rows = displayRows(rules);
  panel.appendChild(buildLintSummary(lint, rows));
  for (const row of buildRuleRows(rows, pageIndex)) panel.appendChild(row);
  appendFooter(panel, rules[0], total, pageIndex);
  return panel;
}

/**
 * Project the persisted rules into the rows the panel actually draws: the
 * four highest-count rules keep a colour each, and the remainder collapses
 * into one neutral aggregate row. `rules[]` arrives sorted by count
 * descending from the linter's cache, so the top four are simply the first
 * four. This also caps the table at five rows — a sixteen-row table is not a
 * default anyone asked for.
 *
 * @param {Array<object>} rules - Persisted per-rule aggregates.
 * @returns {Array<object>} One entry per drawn row, each carrying a `rank`
 *   (its palette index, or null for the aggregate).
 */
function displayRows(rules) {
  const named = rules.slice(0, NAMED_RULE_LIMIT).map((rule, rank) => ({ ...rule, rank }));
  const folded = rules.slice(NAMED_RULE_LIMIT);
  if (folded.length === 0) return named;
  return [...named, { rule: OTHER_RULE, rank: null, count: totalOf(folded), ruleCount: folded.length }];
}

/**
 * The panel is warm-framed only while there is something to act on: a clean
 * run, and a wiki whose lint has never run, keep the neutral panel surface
 * so the warm tint stays a signal rather than a decoration.
 */
function lintPanelClass(total) {
  return `panel lint-panel${total > 0 ? " has-problems" : ""}`;
}

/** The persisted per-rule breakdown, or [] for a cache written before it existed. */
function rulesOf(lint) {
  return Array.isArray(lint.rules) ? lint.rules : [];
}

/** Build the panel head: the title, plus the problem chip once a run exists. */
function buildLintHead(total) {
  const head = el("div", "panel-head");
  const group = el("div", "lint-head-group");
  group.appendChild(el("span", "panel-title", "Lint"));
  if (total !== null) group.appendChild(el("span", "lint-chip", `${total} PROBLEMS`));
  head.appendChild(group);
  return head;
}

/** Build the body band: the two figures on the left, bar + legend on the right. */
function buildLintSummary(lint, rows) {
  const summary = el("div", "lint-summary");
  summary.appendChild(buildTotals(lint));
  const breakdown = el("div", "lint-breakdown");
  breakdown.appendChild(buildStackedBar(rows));
  breakdown.appendChild(buildLegend(rows));
  summary.appendChild(breakdown);
  return summary;
}

/** Build the stacked error/warning figures. */
function buildTotals(lint) {
  const totals = el("div", "lint-totals");
  totals.appendChild(buildFigure("errors", lint.errors ?? 0, "error"));
  totals.appendChild(buildFigure("warnings", lint.warnings ?? 0, "warning"));
  return totals;
}

/** Build one figure row: the count, then its noun pluralised against it. */
function buildFigure(key, count, noun) {
  const row = el("div", "lint-figure-row");
  row.appendChild(el("span", `lint-figure is-${key}`, String(count)));
  // plural() would repeat the count the figure beside it already carries;
  // only the noun's ending is wanted here.
  row.appendChild(el("span", "lint-figure-label", `${noun}${count === 1 ? "" : "s"}`));
  return row;
}

/**
 * Build the stacked proportion bar, one segment per rule. Widths are CSSOM
 * property writes rather than markup `style=` attributes, so the shell's
 * `style-src` needs no `'unsafe-inline'` (the same mechanism the dashboard's
 * citation meter uses).
 */
function buildStackedBar(rows) {
  const bar = el("div", "lint-bar");
  const total = totalOf(rows);
  if (total === 0) return bar;
  // Every rule's count lands in exactly one row — folded rules survive inside
  // the aggregate's sum — so the segments still spend the whole bar.
  for (const row of rows) {
    const segment = rankedEl("div", "lint-bar-seg", undefined, row);
    segment.style.width = `${(row.count / total) * 100}%`;
    bar.appendChild(segment);
  }
  return bar;
}

/** Build the colour legend: a swatch and a "<rule> <count>" label per row. */
function buildLegend(rows) {
  const legend = el("div", "lint-legend");
  for (const row of rows) {
    const item = el("span", "lint-legend-item");
    item.appendChild(rankedEl("span", "lint-swatch", undefined, row));
    item.appendChild(el("span", undefined, `${row.rule} ${row.count}`));
    legend.appendChild(item);
  }
  return legend;
}

/**
 * `el()` plus a row's colour treatment, for the three elements that must
 * agree on it — a bar segment, its legend swatch, and the table's rule name.
 * Ranked rows carry `data-rank`; the aggregate carries a neutral modifier
 * instead of a fifth hue, so no two visible swatches can share a colour.
 */
function rankedEl(tag, className, text, row) {
  if (row.rank === null) return el(tag, `${className} is-other`, text);
  const node = el(tag, className, text);
  node.dataset.rank = String(row.rank);
  return node;
}

/** Sum every row's finding count. */
function totalOf(rows) {
  return rows.reduce((sum, row) => sum + (row.count ?? 0), 0);
}

/** Build the table: a header row plus one row per drawn row, or nothing when clean. */
function buildRuleRows(rows, pageIndex) {
  if (rows.length === 0) return [];
  return [buildHeadRow(), ...rows.map((row) => buildRuleRow(row, pageIndex))];
}

/** Build the table's column-heading row. */
function buildHeadRow() {
  const row = el("div", "lint-row is-head");
  for (const label of ["RULE", "MOST AFFECTED", "COUNT", "FIX"]) {
    row.appendChild(el("span", undefined, label));
  }
  return row;
}

/** Build one table row: name, most-affected summary, count, and the FIX cell. */
function buildRuleRow(row, pageIndex) {
  const element = el("div", "lint-row");
  element.dataset.rule = row.rule;
  element.appendChild(rankedEl("span", "lint-rule", row.rule, row));
  element.appendChild(el("span", "lint-affected", mostAffectedText(row)));
  element.appendChild(el("span", "lint-count", String(row.count ?? 0)));
  // The aggregate has no `topFile`, so it falls through to the same
  // non-routable placeholder infra rules already get — it stands for several
  // rules at once, and there is no one page to send the reader to.
  element.appendChild(buildFixCell(row.topFile, pageIndex));
  return element;
}

/**
 * Describe where a row's findings land. The aggregate row has no file to name
 * — it stands for several rules at once — so it reports its own breadth
 * instead: how many rules it covers.
 */
function mostAffectedText(row) {
  return row.ruleCount === undefined ? affectedFilesText(row) : plural(row.ruleCount, "rule");
}

/**
 * Describe where one rule's findings land. One file carrying a strict
 * majority is worth naming; findings spread evenly are not — "7 files" says
 * more than an arbitrary one of the seven would.
 */
function affectedFilesText(rule) {
  const fileCount = rule.fileCount ?? 0;
  const name = pageNameOf(rule.topFile);
  // A rule confined to one file needs no share: the COUNT column beside it
  // already says how many findings that file holds.
  if (fileCount <= 1) return name;
  if (!hasDominantFile(rule)) return plural(fileCount, "file");
  return `${name} · ${rule.topFileCount} of ${rule.count}`;
}

/** True when one file carries a strict majority of a rule's findings. */
function hasDominantFile(rule) {
  return (rule.topFileCount ?? 0) * 2 > (rule.count ?? 0);
}

/** A file's display name: its basename with the Markdown extension dropped. */
function pageNameOf(file) {
  if (typeof file !== "string" || file.length === 0) return "—";
  return file.split("/").pop().replace(/\.md$/, "");
}

/**
 * Build the FIX cell. A link only when the flagged file resolves to a page
 * this viewer actually renders — infra rules (journal-health,
 * pending-embeddings) flag files that are not wiki pages, and a link to a
 * route that 404s is worse than no link.
 */
function buildFixCell(topFile, pageIndex) {
  const href = pageRouteFor(topFile, pageIndex);
  if (!href) return el("span", "lint-fix is-plain", NO_ROUTE);
  const link = el("a", "lint-fix", "view →");
  link.href = href;
  return link;
}

/** Resolve a lint finding's file to a `#/<dir>/<slug>` route, or null. */
function pageRouteFor(topFile, pageIndex) {
  if (typeof topFile !== "string") return null;
  const match = topFile.match(PAGE_FILE_PATTERN);
  if (!match) return null;
  const [, directory, slug] = match;
  if (!pageIndex.has(`${directory}/${slug}`)) return null;
  return `#/${directory}/${encodeURIComponent(slug)}`;
}

/**
 * Append the footer band naming the rule with the most leverage. "Dominant"
 * means the highest-count rule, so callers pass `rules[0]` — the linter's
 * cache persists the breakdown already sorted by count descending. It is read
 * from the persisted rules, NOT from the folded display rows, so the sentence
 * always names a real rule rather than the aggregate. A clean run has no such
 * rule, so the band is omitted entirely rather than rendered empty.
 */
function appendFooter(panel, dominant, total, pageIndex) {
  if (!dominant || total === 0) return;
  const footer = el("div", "lint-footer");
  const insight = `${dominant.count} of ${total} problems come from ${dominant.rule}.`;
  footer.appendChild(el("span", "lint-insight", insight));
  const action = footerAction(dominant, pageIndex);
  if (action) {
    const link = el("a", "lint-action", action.label);
    link.href = action.href;
    footer.appendChild(link);
  }
  panel.appendChild(footer);
}

/**
 * Resolve where the footer's button goes, and what it says. The insight
 * beside it generalises to whatever rule dominates, so the button must too: a
 * button labelled for the graph explorer that opens a page is the same
 * contradiction in a new place, and one hardcoded destination can only ever
 * suit one rule. Link-graph rules lead to the explorer; everything else leads
 * to the page carrying most of the findings — when that file is one the
 * viewer can actually route to. Returns null when neither resolves, so the
 * band keeps its sentence and drops the button rather than offering a
 * destination that does not follow from it.
 *
 * @param {object} dominant - The highest-count rule aggregate.
 * @param {Set<string>} pageIndex - Routable pages, keyed `"<directory>/<slug>"`.
 * @returns {{href: string, label: string}|null}
 */
function footerAction(dominant, pageIndex) {
  if (LINK_GRAPH_RULES.has(dominant.rule)) {
    return { href: "#/graph", label: "Open the graph explorer" };
  }
  const href = pageRouteFor(dominant.topFile, pageIndex);
  return href ? { href, label: `Open ${pageNameOf(dominant.topFile)}` } : null;
}
