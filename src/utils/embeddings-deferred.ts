/**
 * Keep a last-known usable vector while its replacement is deferred by retry
 * capacity. Preserve only validated v3 records from the active backend and only
 * for pages already discovered as eligible work.
 */

import type { EmbeddingStoreV3, ParsedStore } from "./embeddings-store.js";
import { assertEmbeddingStoreValid } from "./embeddings-validate.js";
import type { PageId } from "./page-id.js";

/** Restore cached page and chunk records without changing their original evidence. */
export function retainDeferredEmbeddings(
  preservable: ParsedStore | null,
  migrated: EmbeddingStoreV3,
  deferred: Set<PageId>,
): void {
  if (!preservable || preservable.version !== 3 || deferred.size === 0) return;
  if (preservable.store.model !== migrated.model) return;
  try {
    assertEmbeddingStoreValid(preservable.store);
  } catch {
    return;
  }

  const old = preservable.store as unknown as EmbeddingStoreV3;
  migrated.entries = [
    ...migrated.entries.filter((entry) => !deferred.has(entry.pageId)),
    ...old.entries.filter((entry) => deferred.has(entry.pageId)),
  ];
  migrated.chunks = [
    ...(migrated.chunks ?? []).filter((entry) => !deferred.has(entry.pageId)),
    ...(old.chunks ?? []).filter((entry) => deferred.has(entry.pageId)),
  ];
}
