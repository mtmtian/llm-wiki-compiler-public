/**
 * A same-backend cached vector remains usable when retry-marker capacity defers
 * its replacement. Strict mode must report only after retry state is settled.
 */

import { readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { afterEach, expect, it, vi } from "vitest";
import * as providerModule from "../src/utils/provider.js";
import { acquireLockBlocking, releaseLock } from "../src/utils/lock.js";
import { refreshEmbeddingsDrainingPending } from "../src/utils/embeddings-refresh.js";
import {
  EMBEDDINGS_FILE,
  MAX_PENDING_EMBEDDING_ATTEMPTS,
  QUARANTINED_EMBEDDINGS_FILE,
} from "../src/utils/constants.js";
import { loadPendingEmbeddings, writePendingEmbeddings } from "../src/utils/pending-embeddings.js";
import { readV3Store } from "./fixtures/v3-store.js";
import { useCompileProject } from "./fixtures/compile-project.js";
import { fullEmbeddingMarker } from "./fixtures/embedding-marker-capacity.js";

const ctx = useCompileProject({ dirSuffix: "deferred-embeddings" });
const PAGE_ID = "concepts/alpha";

/** Make an eligible live concept whose page and body produce persisted vectors. */
async function writeAlpha(revision: number): Promise<void> {
  await writeFile(
    path.join(ctx.dir, "wiki/concepts/alpha.md"),
    `---\ntitle: alpha\nsummary: alpha revision ${revision}\n---\n\nAlpha content revision ${revision}.\n`,
  );
}

/** Run the shared drain under its documented project-lock precondition. */
async function refresh(pageIds: string[]): Promise<void> {
  await acquireLockBlocking(ctx.dir);
  try {
    await refreshEmbeddingsDrainingPending(ctx.dir, pageIds);
  } finally {
    await releaseLock(ctx.dir);
  }
}

/** Stub the OpenAI embedding provider and return its batch-call spy. */
function stubProvider(): ReturnType<typeof vi.fn> {
  process.env.LLMWIKI_PROVIDER = "openai";
  process.env.OPENAI_API_KEY = "test-key";
  process.env.LLMWIKI_EMBEDDING_MODEL = "test-embed";
  const embedBatch = vi.fn(async (texts: string[]) => texts.map(() => [0.5, 0.5]));
  vi.spyOn(providerModule, "getProvider").mockReturnValue({
    embed: async () => [0.5, 0.5],
    embedBatch,
  } as unknown as ReturnType<typeof providerModule.getProvider>);
  return embedBatch;
}

afterEach(() => {
  delete process.env.OPENAI_API_KEY;
  delete process.env.LLMWIKI_EMBEDDING_MODEL;
  delete process.env.LLMWIKI_EMBED_STRICT;
});

it("retains valid page/chunk cache while deferring capacity overflow, then settles before strict error", async () => {
  const embedBatch = stubProvider();

  await writeAlpha(1);
  await refresh([PAGE_ID]);
  const oldCache = (await readV3Store(ctx.dir))!;
  expect(oldCache.entries.some((entry) => entry.pageId === PAGE_ID)).toBe(true);
  expect(oldCache.chunks?.some((entry) => entry.pageId === PAGE_ID)).toBe(true);

  await writeAlpha(2);
  const full = fullEmbeddingMarker("count", 1);
  await writePendingEmbeddings(ctx.dir, full);
  const storePath = path.join(ctx.dir, EMBEDDINGS_FILE);
  const before = { bytes: await readFile(storePath), mtime: (await stat(storePath)).mtimeMs };
  embedBatch.mockClear();
  process.env.LLMWIKI_EMBED_STRICT = "on";

  await expect(refresh([PAGE_ID])).rejects.toThrow("1 page(s) deferred");

  expect(embedBatch).not.toHaveBeenCalled();
  const stored = (await readV3Store(ctx.dir))!;
  expect(stored.entries.filter((entry) => entry.pageId === PAGE_ID)).toEqual(
    oldCache.entries.filter((entry) => entry.pageId === PAGE_ID),
  );
  expect(stored.chunks?.filter((entry) => entry.pageId === PAGE_ID)).toEqual(
    oldCache.chunks?.filter((entry) => entry.pageId === PAGE_ID),
  );
  expect(await readFile(storePath)).toEqual(before.bytes);
  expect((await stat(storePath)).mtimeMs).toBe(before.mtime);
  expect((await loadPendingEmbeddings(ctx.dir))[0]?.attempts).toBe(2);
  expect(await loadPendingEmbeddings(ctx.dir)).not.toContainEqual({ pageId: PAGE_ID, attempts: MAX_PENDING_EMBEDDING_ATTEMPTS });
});

it("does not call the provider for a quarantined page and retains its same-backend cache", async () => {
  const embedBatch = stubProvider();
  await writeAlpha(1);
  await refresh([PAGE_ID]);
  const oldCache = (await readV3Store(ctx.dir))!;
  const oldStoreBytes = await readFile(path.join(ctx.dir, EMBEDDINGS_FILE));
  await writeAlpha(2);
  await writePendingEmbeddings(ctx.dir, [{ pageId: PAGE_ID, attempts: MAX_PENDING_EMBEDDING_ATTEMPTS }], QUARANTINED_EMBEDDINGS_FILE);
  embedBatch.mockClear();

  await refresh([]);

  expect(embedBatch).not.toHaveBeenCalled();
  expect((await readV3Store(ctx.dir))!.entries.filter((entry) => entry.pageId === PAGE_ID)).toEqual(
    oldCache.entries.filter((entry) => entry.pageId === PAGE_ID),
  );
  expect(await readFile(path.join(ctx.dir, EMBEDDINGS_FILE))).toEqual(oldStoreBytes);
});
