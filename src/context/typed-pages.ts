/**
 * @file src/context/typed-pages.ts
 * @description The context pool's OWN policy over a non-default profile's typed
 * entity pages: `retrieval.includeInContext`.
 *
 * The viewer snapshot already carries every profile-VALID typed entity page in
 * `snapshot.pages` — that is what makes them lexically rankable (by prompt match,
 * no embeddings needed), selectable as primaries with their body, and reachable
 * through the snapshot graph's relation edges. This module used to splice them in
 * itself, from a second `collectEntityPages` read; doing that on top of a snapshot
 * that already has them would list every typed page twice, so the job here is now
 * the one thing the SNAPSHOT must not do — apply a retrieval policy.
 *
 * That separation is the point. A page an entity type keeps out of CONTEXT is
 * still a page a user can open in the viewer, so the exclusion belongs to the
 * context pool, not to the shared page list.
 *
 * `includeInContext` is tri-state: an explicit `false` drops that type's pages
 * from the pool; omitted or `true` keeps them. A DEFAULT project has no typed
 * pages at all, so the pool is the snapshot's own list and the default context
 * pack is byte-identical. Profile-INVALID pages were already excluded upstream by
 * the SHARED `invalidEntityPagePaths`, so an unvalidated page is never promoted
 * as clean primary evidence.
 */

import { resolveNonDefaultProfile, type PreloadedProfile } from "../profile/block.js";
import type { ProfilePack } from "../profile/types.js";
import type { ViewerPage, ViewerSnapshot } from "../viewer/types.js";

/**
 * Return a snapshot whose `pages` pool honours each entity type's
 * `retrieval.includeInContext`.
 *
 * The ORIGINAL snapshot object is returned unchanged whenever nothing is
 * excluded (the built-in default profile, a read error, or a profile that opts
 * nothing out), so the common path allocates nothing and the default context
 * pack is byte-identical.
 *
 * @param root - Absolute project root.
 * @param snapshot - The frozen viewer snapshot to scope.
 * @param preloaded - A profile the caller already resolved, threaded through so
 *   one pack is never assembled against two mid-swapped profiles.
 * @returns The snapshot, minus any typed page its entity type keeps out of context.
 */
export async function narrowSnapshotToContextPool(
  root: string,
  snapshot: ViewerSnapshot,
  preloaded?: PreloadedProfile,
): Promise<ViewerSnapshot> {
  let profile: ProfilePack;
  try {
    const loaded = await resolveNonDefaultProfile(root, preloaded);
    if (loaded === undefined) return snapshot; // default profile → byte-identical pool
    profile = loaded.profile;
  } catch {
    return snapshot; // a profile-load failure must not break context
  }
  const pages = snapshot.pages.filter((page) => !isExcludedFromContext(page, profile));
  if (pages.length === snapshot.pages.length) return snapshot;
  return { ...snapshot, pages };
}

/**
 * True when `page` is a typed entity page whose type declares
 * `retrieval.includeInContext: false`. Default pages are never excluded — the
 * flag lives on an entity type definition, and they have none.
 */
function isExcludedFromContext(page: ViewerPage, profile: ProfilePack): boolean {
  if (page.entityType === undefined) return false;
  return profile.entities[page.entityType]?.retrieval?.includeInContext === false;
}
