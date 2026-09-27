/**
 * llmwiki viewer — the typed list route's namespace.
 *
 * A profile's entity types are addressable in the viewer's URL space, and that
 * space is not empty: the viewer owns `#/sources`, `#/reviews`, `#/graph` and
 * the rest. The shipped `autosci` template declares entity types named
 * `sources` and `reviews`, so a bare `#/<entity-type>` list route sent those two
 * type rows to the viewer's own surfaces instead of the type's pages.
 *
 * The fix is to namespace the LIST route rather than to reserve the viewer's
 * route names in the profile schema. Reserving them would break a shipped
 * template, need a migration, and tie the schema to the viewer's route list
 * forever — every new route becoming a breaking profile change. The viewer's URL
 * space is the viewer's problem, so the viewer moves.
 *
 * WHY `_type` AND NOT `type`: a namespaced hash is two segments, so it also
 * matches the `#/<directory>/<slug>` page pattern. With a bare `type/` prefix a
 * profile could legally declare an entity type named `type`, and its pages would
 * then live at exactly the hashes the list routes claim. Entity type keys must
 * be slug-safe — `^[a-z0-9][a-z0-9-]*$`, see `isSlugSafe` in
 * src/profile/identity.ts — and a leading underscore is outside that grammar, so
 * `_type` is a name no profile can ever declare. The ambiguity is removed by
 * construction rather than by a reserved word or a documented caveat, and the
 * server would reject `/api/page/_type/<slug>` for the same reason.
 *
 * Page routes are NOT namespaced: `#/<entity-type>/<slug>` is already validated
 * against the declared types server-side (`isAllowedDirectory`, api-pages.ts),
 * so it has no collision to fix.
 *
 * Three modules read the same form — the sidebar builds the href
 * (viewer-nav-types.js), the router resolves it (viewer.js), the highlight
 * matches it (viewer-sidebar.js) — so the form lives here once.
 */

/**
 * The first segment of every typed list route. Not a legal entity type name;
 * see the file header for why that is the whole point. Module-private: callers
 * build and read the form through the two functions below, so the prefix is
 * spelled once in the codebase.
 */
const TYPE_LIST_PREFIX = "_type";

/** `#/_type/<entity-type>`, built FROM the prefix so form and parser cannot drift. */
const TYPE_LIST_HASH_PATTERN = new RegExp(`^#/${TYPE_LIST_PREFIX}/([^/]+)$`);

/**
 * The hash a profile's type row navigates to.
 *
 * @param {string} type - The declared entity type id.
 * @returns {string} Its list route hash.
 */
export function typeListHref(type) {
  return `#/${TYPE_LIST_PREFIX}/${encodeURIComponent(type)}`;
}

/**
 * The entity type a namespaced list hash names, or null when the hash is not
 * one at all. Says nothing about whether that type is DECLARED — the envelope
 * answers that, and only the router has it.
 *
 * Malformed percent-encoding reads as "not a list hash" rather than throwing, so
 * a hand-edited URL falls through to the router's ordinary home fallback.
 *
 * @param {string|null|undefined} hash - A `location.hash` value.
 * @returns {string|null} The entity type id, or null.
 */
export function typeListHashType(hash) {
  const match = String(hash ?? "").match(TYPE_LIST_HASH_PATTERN);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}
