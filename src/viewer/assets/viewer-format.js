/**
 * llmwiki viewer — shared value formatting and derivation.
 *
 * Pure functions over payload values, with no DOM access. These are shared
 * because more than one render module needs the same answer, and two copies
 * of an age calculation drift the moment one of them is fixed.
 */

/** Milliseconds in a day, for whole-day age arithmetic. */
const DAY_MS = 86_400_000;

/** Milliseconds in a minute and an hour, for sub-day recency arithmetic. */
const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;

/** Shortest elapsed time worth naming; anything below reads as "moments". */
const MOMENTS = "moments";

/** Fallback project title shown until a real one is known. */
const DEFAULT_TITLE = "llmwiki";

/**
 * Display title for an `/api/pages` envelope, with a stable fallback.
 *
 * Shared by viewer.js (dashboard heading) and viewer-header.js (persistent
 * header identity) so the two surfaces cannot disagree about the title.
 *
 * @param {unknown} envelope - The bootstrap envelope; accessed defensively
 *   since first paint can call this before the fetch settles.
 * @returns {string}
 */
export function projectTitle(envelope) {
  return envelope?.project?.title || DEFAULT_TITLE;
}

/**
 * Whole-day age of an ISO timestamp, e.g. "3d". Returns "today" for anything
 * less than a day old and "" when the timestamp is absent or unparseable —
 * an empty cell reads better than "NaNd".
 *
 * @param {unknown} iso - ISO-8601 timestamp.
 * @returns {string}
 */
// CRAP is estimated from zero call-graph references into this module:
// test/viewer-format.test.ts reads this file's source and evals it directly
// (JSDOM's eval does not drive ES-module loading — see
// test/fixtures/viewer-jsdom.ts for the same constraint applied to the
// shared harness), so static analysis cannot see that every branch below is
// exercised by that file's "relativeAge" describe block. Not missing tests,
// and not missing callers either — viewer-lists.js and viewer-dashboard.js
// both import this directly; fallow just cannot trace either edge.
// fallow-ignore-next-line complexity
export function relativeAge(iso) {
  if (typeof iso !== "string" || iso.length === 0) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const days = Math.floor((Date.now() - then) / DAY_MS);
  if (days <= 0) return "today";
  return `${days}d`;
}

/**
 * Render an ISO timestamp as `YYYY-MM-DD HH:MM`, or "unknown" when absent.
 *
 * Shared by the persistent header's meta line and the health screen's
 * lint-last-run caption so the two cannot disagree about how a snapshot
 * time is spelled. Slicing the ISO string keeps the value in UTC — the
 * timestamps this renders are produced in UTC, and `toLocaleString` would
 * silently shift them into the reader's zone without saying so.
 *
 * @param {unknown} iso - ISO-8601 timestamp.
 * @returns {string}
 */
export function formatUtcTimestamp(iso) {
  if (typeof iso !== "string" || iso.length < 16) return "unknown";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

/**
 * Elapsed time since an ISO timestamp in the largest unit that still reads
 * as a whole number — "28 min", "3 h", "2 d" — or "" when the timestamp is
 * absent or unparseable, so a caller can drop the phrase rather than print
 * "NaN ago".
 *
 * Distinct from {@link relativeAge}, which answers a different question:
 * that one buckets page edits into whole days ("today", "3d") for list
 * rows, where sub-day precision is noise. A lint run minutes old needs the
 * minutes, so this reports them.
 *
 * @param {unknown} iso - ISO-8601 timestamp.
 * @returns {string}
 */
// Same call-graph blind spot as relativeAge below — see that function's comment.
// fallow-ignore-next-line complexity
export function relativeSince(iso) {
  if (typeof iso !== "string" || iso.length === 0) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const elapsed = Date.now() - then;
  if (elapsed < MINUTE_MS) return MOMENTS;
  if (elapsed < HOUR_MS) return `${Math.floor(elapsed / MINUTE_MS)} min`;
  if (elapsed < DAY_MS) return `${Math.floor(elapsed / HOUR_MS)} h`;
  return `${Math.floor(elapsed / DAY_MS)} d`;
}

/**
 * Compose a freshness badge label from the two actionable counts, e.g.
 * "2 STALE · 1 ORPHANED". `calmLabel` is what the badge reads when neither
 * count has anything to report — the health screen's Freshness panel says
 * "IN SYNC" — so the wording stays the calling surface's own choice while
 * the composition rule stays shared. The persistent header was the second
 * caller until its pill stopped reporting freshness alone and became the
 * whole-wiki verdict (see viewer-header.js); `calmLabel` stays a parameter
 * because the wording is a surface's decision, not this function's.
 *
 * @param {number} stale
 * @param {number} orphaned
 * @param {string} calmLabel
 * @returns {string}
 */
export function freshnessBadgeText(stale, orphaned, calmLabel) {
  const parts = [];
  if (stale > 0) parts.push(`${stale} STALE`);
  if (orphaned > 0) parts.push(`${orphaned} ORPHANED`);
  return parts.length === 0 ? calmLabel : parts.join(" · ");
}

/**
 * Total lint findings, or null when lint has never run.
 *
 * A null cache is NOT reported as zero: "no lint run" and "a clean lint run"
 * are different facts, and returning 0 for both would assert the stronger one.
 * Callers decide how to render null — the sidebar omits its badge, the compile
 * receipt says "never run".
 *
 * @param {unknown} lint - The `/api/health` lint cache entry, or null.
 * @returns {number|null}
 */
// Same call-graph blind spot as relativeAge above — see that function's
// comment.
// fallow-ignore-next-line complexity
export function lintTotal(lint) {
  if (!lint || typeof lint !== "object") return null;
  const warnings = typeof lint.warnings === "number" ? lint.warnings : 0;
  const errors = typeof lint.errors === "number" ? lint.errors : 0;
  return warnings + errors;
}

/**
 * Freshness statuses that earn the warning dot. Only `"stale"` and
 * `"orphaned"` are actionable; `"fresh"` and `"unverified"` both read as
 * calm — an unverified page (freshness could not be computed, e.g. a
 * missing or corrupt state.json) is not evidence of a problem with the
 * page itself, so it must not warn.
 *
 * Single source of truth for every freshness dot in the viewer. The
 * dashboard and the list routes each once hardcoded their own version of
 * this rule and quietly disagreed about what "unverified" should render
 * as — every consumer now imports this instead of re-deriving it.
 */
const WARN_FRESHNESS_STATUSES = new Set(["stale", "orphaned"]);

/**
 * True when a freshness status should render the warning dot rather than
 * the calm one.
 *
 * @param {unknown} status - A `FreshnessStatus` value, or anything else —
 *   handled defensively so a malformed payload never throws.
 * @returns {boolean}
 */
export function isWarnFreshness(status) {
  return WARN_FRESHNESS_STATUSES.has(status);
}

/**
 * Pluralise a noun by count, e.g. `plural(1, "dangling target")` → "1
 * dangling target", `plural(11, "dangling target")` → "11 dangling
 * targets". Every count-bearing string in the viewer routes through this —
 * originally the graph explorer's node tooltip had its own private copy,
 * and the dashboard wrote several counts as hardcoded-plural template
 * literals that read wrong at exactly 1 (e.g. "1 dangling targets"); both
 * now call this instead of a second pluraliser that could disagree with it
 * at the n=1 boundary.
 *
 * @param {number} count
 * @param {string} noun - Singular form of the noun.
 * @returns {string}
 */
export function plural(count, noun) {
  return `${count} ${noun}${count !== 1 ? "s" : ""}`;
}
