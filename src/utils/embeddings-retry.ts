/**
 * Durable retry bookkeeping for embedding refreshes. A page is admitted to the
 * provider only after its retry budget is present in the confined marker. Exhausted
 * entries remain excluded even when the separate quarantine marker is full.
 */

import {
  MAX_PENDING_EMBEDDING_ATTEMPTS,
  QUARANTINED_EMBEDDINGS_FILE,
} from "./constants.js";
import type { PageId } from "./page-id.js";
import {
  mergeFreshAttempts,
  readPendingMarker,
  settleAfterFailure,
  settleAfterSuccess,
  warnQuarantined,
  writePendingEmbeddings,
  type PendingEmbedding,
  type SettleResult,
} from "./pending-embeddings.js";

/** An entry at the retry limit must never be sent to the provider again. */
function exhausted(entry: PendingEmbedding): boolean {
  return entry.attempts >= MAX_PENDING_EMBEDDING_ATTEMPTS;
}

/** Retry state shared by discovery, provider execution, and durable settlement. */
class EmbeddingRetry {
  /** Eligible pages that were discovered but could not be admitted by marker capacity. */
  deferred: PageId[] = [];
  private quarantineUnavailable = false;

  constructor(
    private readonly root: string,
    private readonly pending: PendingEmbedding[],
    private quarantined: PendingEmbedding[],
    private readonly changedPageIds: PageId[],
    private readonly releaseQuarantineIds: Set<PageId>,
    quarantineUnavailable: boolean,
  ) {
    this.quarantineUnavailable = quarantineUnavailable;
  }

  /** Active durable budgets only; exhausted or quarantined ids are excluded. */
  get pageIds(): PageId[] {
    if (this.quarantineUnavailable) return [];
    const blocked = new Set(this.quarantined.map((entry) => entry.pageId));
    return this.pending.filter((entry) => !exhausted(entry) && !blocked.has(entry.pageId))
      .map((entry) => entry.pageId);
  }

  /** Persist the known intent before discovery or provider work can fail. */
  async recordPending(): Promise<void> {
    const before = await readPendingMarker(this.root);
    if (this.pending.length > 0 || before.status === "ok") {
      await writePendingEmbeddings(this.root, this.pending);
    }
    const stored = await readPendingMarker(this.root);
    this.replacePending(stored.status === "ok" ? stored.entries : []);
    await this.releaseExplicitlyChangedQuarantines();
  }

  /** Add discovered pages, then return only ids whose budgets survived both caps. */
  async prepare(discovered: PageId[]): Promise<PageId[]> {
    const quarantinedIds = new Set(this.quarantined.map((entry) => entry.pageId));
    const exhaustedIds = new Set(this.pending.filter(exhausted).map((entry) => entry.pageId));
    const allowed = discovered.filter((id) => !quarantinedIds.has(id) && !exhaustedIds.has(id));
    const charged = this.pending.filter((entry) => entry.attempts > 0).map((entry) => entry.pageId);
    this.replacePending(mergeFreshAttempts(this.pending, [...charged, ...this.changedPageIds, ...allowed]));
    await this.recordPending();

    const persisted = new Set(this.pending.map((entry) => entry.pageId));
    const withheld = this.quarantineUnavailable ? allowed : allowed.filter((id) => !persisted.has(id));
    this.deferred = [...new Set([...this.deferred, ...withheld])];
    const stillBlocked = new Set(this.quarantined.map((entry) => entry.pageId));
    return allowed.filter((id) => persisted.has(id) && !stillBlocked.has(id) && !this.quarantineUnavailable);
  }

  /** Settle only work whose retry state was persisted before provider execution. */
  async succeed(embedded: PageId[], eligible: PageId[]): Promise<void> {
    await this.settle(settleAfterSuccess(this.pending.filter((entry) => !exhausted(entry)), embedded, eligible));
  }

  /** Count a failed provider pass, retaining exhausted ids if quarantine cannot store them. */
  async fail(): Promise<void> {
    await this.settle(settleAfterFailure(this.pending.filter((entry) => !exhausted(entry)), this.pageIds));
  }

  /** Persist quarantine exclusions before removing their entries from pending. */
  private async settle(result: SettleResult): Promise<void> {
    const retiring = [...this.pending.filter(exhausted), ...result.quarantined];
    const marker = await readPendingMarker(this.root, QUARANTINED_EMBEDDINGS_FILE);
    if (marker.status === "unavailable") {
      this.quarantineUnavailable = true;
      await this.persistPending([...retiring, ...result.survivors]);
      warnQuarantined(result.quarantined);
      return;
    }

    const combinedById = new Map<string, PendingEmbedding>();
    for (const entry of [...marker.entries, ...retiring]) {
      const prior = combinedById.get(entry.pageId);
      combinedById.set(entry.pageId, {
        pageId: entry.pageId,
        attempts: Math.max(prior?.attempts ?? 0, entry.attempts),
      });
    }
    const combined = [...combinedById.values()];
    if (retiring.length > 0) await writePendingEmbeddings(this.root, combined, QUARANTINED_EMBEDDINGS_FILE);
    const savedQuarantine = await readPendingMarker(this.root, QUARANTINED_EMBEDDINGS_FILE);
    if (savedQuarantine.status === "unavailable") {
      this.quarantineUnavailable = true;
      await this.persistPending([...retiring, ...result.survivors]);
      warnQuarantined(result.quarantined);
      return;
    }

    this.quarantined = savedQuarantine.entries;
    const durable = new Set(this.quarantined.map((entry) => entry.pageId));
    const held = retiring.filter((entry) => !durable.has(entry.pageId));
    const survivors = prioritizeBudgets(result.survivors);
    await this.persistPending([...held, ...survivors]);
    warnQuarantined(result.quarantined);
  }

  /** Persist and reload pending state so in-memory work never exceeds disk capacity. */
  private async persistPending(entries: PendingEmbedding[]): Promise<void> {
    const ordered = prioritizeBudgets(entries);
    const prior = await readPendingMarker(this.root);
    if (ordered.length > 0 || prior.status === "ok") await writePendingEmbeddings(this.root, ordered);
    const marker = await readPendingMarker(this.root);
    this.replacePending(marker.status === "ok" ? marker.entries : []);
  }

  /** Remove ids from quarantine only after their changed-page budgets are durable. */
  private async releaseExplicitlyChangedQuarantines(): Promise<void> {
    if (this.releaseQuarantineIds.size === 0) return;
    const pendingIds = new Set(this.pending.map((entry) => entry.pageId));
    const releasable = new Set([...this.releaseQuarantineIds].filter((id) => pendingIds.has(id)));
    this.deferred = [...new Set([
      ...this.deferred,
      ...[...this.releaseQuarantineIds].filter((id) => !pendingIds.has(id)),
    ])];
    if (releasable.size === 0) return;
    const marker = await readPendingMarker(this.root, QUARANTINED_EMBEDDINGS_FILE);
    if (marker.status === "unavailable") {
      this.quarantineUnavailable = true;
      return;
    }
    const remaining = marker.entries.filter((entry) => !releasable.has(entry.pageId));
    if (remaining.length !== marker.entries.length) {
      await writePendingEmbeddings(this.root, remaining, QUARANTINED_EMBEDDINGS_FILE);
    }
    const saved = await readPendingMarker(this.root, QUARANTINED_EMBEDDINGS_FILE);
    if (saved.status === "unavailable") {
      this.quarantineUnavailable = true;
      return;
    }
    this.quarantined = saved.entries;
  }

  private replacePending(entries: PendingEmbedding[]): void {
    this.pending.splice(0, this.pending.length, ...entries);
  }
}

/** Keep previously charged budgets and exhausted exclusions ahead of fresh backlog. */
function prioritizeBudgets(entries: PendingEmbedding[]): PendingEmbedding[] {
  return [...entries.filter((entry) => entry.attempts > 0), ...entries.filter((entry) => entry.attempts === 0)];
}

/** Load durable retry state; changed pages may explicitly release their quarantine. */
export async function loadEmbeddingRetry(root: string, changedPageIds: PageId[]): Promise<EmbeddingRetry> {
  const [pendingMarker, quarantineMarker] = await Promise.all([
    readPendingMarker(root),
    readPendingMarker(root, QUARANTINED_EMBEDDINGS_FILE),
  ]);
  const fresh = new Set(changedPageIds);
  const quarantined = quarantineMarker.status === "ok" ? quarantineMarker.entries : [];
  const quarantinedIds = new Set(quarantined.map((entry) => entry.pageId));
  const pending = (pendingMarker.status === "ok" ? pendingMarker.entries : [])
    .filter((entry) => !quarantinedIds.has(entry.pageId))
    .filter((entry) => !(exhausted(entry) && fresh.has(entry.pageId)));
  const charged = pending.filter((entry) => entry.attempts > 0).map((entry) => entry.pageId);
  const ordered = mergeFreshAttempts(pending, [...charged, ...changedPageIds]);
  return new EmbeddingRetry(
    root,
    ordered,
    quarantined,
    changedPageIds,
    new Set(changedPageIds.filter((id) => quarantinedIds.has(id))),
    quarantineMarker.status === "unavailable",
  );
}
