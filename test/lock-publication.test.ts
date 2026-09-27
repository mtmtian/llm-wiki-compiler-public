/**
 * Exercise lock publication against real files with one controlled write pause.
 * A contender must never reclaim a lock whose owner record is still being
 * written: that lets both callers enter the protected mutation concurrently.
 */
import { afterEach, expect, it, vi } from "vitest";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { acquireLock, releaseLock } from "../src/utils/lock.js";

const writes = vi.hoisted(() => ({
  beforeWrite: undefined as (() => Promise<void>) | undefined,
  beforeUnlink: undefined as ((file: string) => Promise<void>) | undefined,
}));

vi.mock("fs/promises", async (importOriginal) => {
  const fs = await importOriginal<typeof import("fs/promises")>();
  return {
    ...fs,
    unlink: async (file: Parameters<typeof fs.unlink>[0]) => {
      await writes.beforeUnlink?.(String(file));
      return fs.unlink(file);
    },
    open: async (...args: Parameters<typeof fs.open>) => {
      const handle = await fs.open(...args);
      const write = handle.writeFile.bind(handle);
      handle.writeFile = async (...contents: Parameters<typeof handle.writeFile>) => {
        await writes.beforeWrite?.();
        return write(...contents);
      };
      return handle;
    },
  };
});

let root = "";
afterEach(async () => {
  writes.beforeWrite = undefined;
  writes.beforeUnlink = undefined;
  if (root) await rm(root, { recursive: true, force: true });
});

/** A manually released barrier controls the race without scheduling sleeps. */
function barrier(): { promise: Promise<void>; resolve: () => void } {
  let resolve = () => {};
  const promise = new Promise<void>((release) => { resolve = release; });
  return { promise, resolve };
}

it("publishes only a complete owner record, with exactly one winning contender", async () => {
  root = await mkdtemp(path.join(tmpdir(), "lock-publication-"));
  const paused = barrier();
  const resumed = barrier();
  writes.beforeWrite = async () => {
    writes.beforeWrite = undefined;
    paused.resolve();
    await resumed.promise;
  };
  const first = acquireLock(root, { quiet: true });
  let second = false;
  try {
    await paused.promise;
    second = await acquireLock(root, { quiet: true });
  } finally {
    resumed.resolve();
  }
  const winners = [await first, second].filter(Boolean);
  await releaseLock(root);
  expect(winners).toHaveLength(1);
  expect(await readdir(path.join(root, ".llmwiki"))).toEqual([]);
});

it("leaves no published lock or candidate when writing the owner fails", async () => {
  root = await mkdtemp(path.join(tmpdir(), "lock-write-failure-"));
  writes.beforeWrite = async () => { throw new Error("injected owner write failure"); };
  await expect(acquireLock(root)).rejects.toThrow("injected owner write failure");
  expect(await readdir(path.join(root, ".llmwiki"))).toEqual([]);
});

it("reports a published lock as acquired even when candidate cleanup fails", async () => {
  root = await mkdtemp(path.join(tmpdir(), "lock-cleanup-failure-"));
  writes.beforeUnlink = async (file) => {
    if (file.endsWith(".tmp")) throw new Error("injected cleanup failure");
  };
  expect(await acquireLock(root)).toBe(true);
  expect(await acquireLock(root, { quiet: true })).toBe(false);
  await releaseLock(root);
  writes.beforeUnlink = undefined;
  expect(await acquireLock(root)).toBe(true);
  await releaseLock(root);
});
