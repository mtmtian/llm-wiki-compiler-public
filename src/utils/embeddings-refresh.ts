/**
 * @file src/utils/embeddings-refresh.ts
 * @description The SINGLE pending-marker-draining embeddings refresh both the
 * compiler and `review approve` route through, so the per-id write-ahead
 * lifecycle is never re-implemented (and never partially omitted) per call site.
 *
 * ## Why one shared drain
 * The compiler's post-write refresh and the `review approve` post-write refresh
 * are the SAME operation: union the freshly-changed page-ids into any prior
 * pending entries, write the intent ahead, run the lock-free embeddings core,
 * then settle the marker per-id (clear embedded, retain eligible-unembedded,
 * quarantine ineligible-over-cap). A separate review-approve refresh that only
 * called the core for the approved id NEVER drained the accumulated marker — so a
 * project run purely as `compile --review` + `review approve` leaked pending ids
 * that were never retried, leaving embeddings stale indefinitely. Folding both
 * onto this function closes that gap by construction.
 *
 * ## Lock precondition (caller MUST hold the project lock)
 * This calls {@link updateEmbeddingsLockedCore}, the LOCK-FREE core, NOT the
 * self-locking wrapper. Both call sites already hold `.llmwiki/lock` across the
 * call (compile for its whole pipeline; `review approve` via `runReviewUnderLock`),
 * so re-locking here would deadlock. Any new caller MUST likewise hold the lock.
 *
 * ## Non-fatal
 * Embeddings are a non-critical enhancement: a missing API key or a transient
 * provider error settles the marker for a retry and warns rather than throwing,
 * so an embeddings failure can never break a compile or an approval.
 */

import { updateEmbeddingsLockedCore } from "./embeddings.js";
import { handleSafeEmbeddingFailure } from "./embeddings-batch.js";
import { verbose } from "./output.js";
import type { PageId } from "./page-id.js";
import { loadEmbeddingRetry } from "./embeddings-retry.js";

/**
 * Refresh embeddings for `changedPageIds` while DRAINING the durable pending
 * marker, then settle that marker per-id. The full write-ahead lifecycle:
 *
 *  1. Load pending and quarantined state, then persist explicit changes before work.
 *  2. Let the core discover stale/missing eligible pages and persist their budgets
 *     before it calls the embedding provider.
 *  3. Settle only durable work; pages refused by count/byte capacity stay deferred.
 *
 * @param root - Absolute project root the marker is confined under.
 * @param changedPageIds - Qualified page-ids changed this run (may be empty —
 *   the prior pending entries are still drained).
 * @precondition The caller MUST hold the project lock across this call.
 */
export async function refreshEmbeddingsDrainingPending(
  root: string,
  changedPageIds: PageId[],
): Promise<void> {
  const retry = await loadEmbeddingRetry(root, changedPageIds);
  verbose(`embeddings: refreshing ${retry.pageIds.length} page-id(s)`);
  await retry.recordPending();
  let failed = false;
  let failure: unknown;
  try {
    const { embedded, eligible } = await updateEmbeddingsLockedCore(
      root,
      retry.pageIds,
      (pageIds) => retry.prepare(pageIds),
    );
    await retry.succeed(embedded, eligible);
  } catch (err) {
    await retry.fail();
    failed = true;
    failure = err;
  }
  finishRefresh(failed, failure, retry.deferred);
}

/** Emit capacity diagnostics after durable settlement, preserving strict-mode errors. */
function finishRefresh(failed: boolean, failure: unknown, deferred: PageId[]): void {
  let strictError: unknown;
  let hasStrictError = false;
  if (failed) {
    const message = failure instanceof Error ? failure.message : String(failure);
    try {
      handleSafeEmbeddingFailure(failure, `Skipped embeddings update: ${message}`);
    } catch (err) {
      strictError = err;
      hasStrictError = true;
    }
  }
  try {
    reportDeferredWork(deferred);
  } catch (err) {
    if (!hasStrictError) {
      strictError = err;
      hasStrictError = true;
    }
  }
  if (hasStrictError) throw strictError;
}

/** Report deferred work only after pending and quarantine markers have settled. */
function reportDeferredWork(deferred: PageId[]): void {
  if (deferred.length === 0) return;
  const maxWarningIds = 10;
  const ids = deferred.length <= maxWarningIds ? ` (${deferred.join(", ")})` : "";
  const message = `${deferred.length} page(s) deferred${ids}: embedding retry marker at capacity or unavailable. ` +
    "Free retry-marker capacity or fix marker storage, then run compile again; these pages were not attempted.";
  handleSafeEmbeddingFailure(new Error(message), message);
}
