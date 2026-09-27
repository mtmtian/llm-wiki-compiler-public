/**
 * Source snapshot cursor regression tests.
 *
 * Extraction sends one exact source snapshot to the model. If the source is
 * edited while that request is pending, persisted state must still point at
 * the snapshot that was consumed so the next compile notices the edit.
 */

import { describe, expect, it, vi } from "vitest";
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { AnthropicProvider } from "../src/providers/anthropic.js";
import { compileAndReport } from "../src/compiler/index.js";
import { hashFile } from "../src/compiler/hasher.js";
import { readState } from "../src/utils/state.js";
import { useCompileProject } from "./fixtures/compile-project.js";

const EMPTY_SUCCESS = JSON.stringify({
  disposition: "no-durable-knowledge",
  reason: "Only a completed status update remains.",
  concepts: [],
});

const NORMAL_EXTRACTION = JSON.stringify({
  concepts: [{ concept: "Snapshot Topic", summary: "A durable fact.", is_new: true }],
});

interface HeldCompile {
  run: ReturnType<typeof compileAndReport>;
  calls: string[];
  oldHash: string;
  release: () => void;
}

/** Create a one-shot promise gate for deterministic provider interleaving. */
function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
} {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((finish) => { resolve = finish; });
  return { promise, resolve };
}

/** Start compile, pausing the first extraction after its source was read. */
async function startHeldCompile(root: string, response: string): Promise<HeldCompile> {
  const sourcePath = path.join(root, "sources", "source.md");
  const oldHash = await hashFile(sourcePath);
  const gate = deferred<void>();
  const started = deferred<void>();
  const calls: string[] = [];
  vi.spyOn(AnthropicProvider.prototype, "toolCall").mockImplementation(async (system: string) => {
    calls.push(system);
    if (calls.length === 1) {
      started.resolve(undefined);
      await gate.promise;
    }
    return response;
  });
  const run = compileAndReport(root);
  await started.promise;
  return { run, calls, oldHash, release: () => gate.resolve(undefined) };
}

describe("compiler source snapshot cursor", () => {
  const ctx = useCompileProject({
    dirSuffix: "source-snapshot-cursor",
    sourceFile: "source.md",
    sourceContent: "# Original\n\nOriginal bytes are consumed.",
  });

  it.each([
    ["explicit empty", EMPTY_SUCCESS],
    ["durable", NORMAL_EXTRACTION],
  ])("reprocesses an edited source after an in-flight %s extraction", async (_label, response) => {
    vi.spyOn(console, "log").mockImplementation(() => {});
    vi.spyOn(AnthropicProvider.prototype, "complete").mockResolvedValue(
      "Snapshot page body. ^[source.md]",
    );
    const pending = await startHeldCompile(ctx.dir, response);
    await writeFile(
      path.join(ctx.dir, "sources", "source.md"),
      "# Edited\n\nEdited bytes arrived during the model request.",
      "utf-8",
    );
    pending.release();
    expect((await pending.run).errors).toEqual([]);

    const firstState = await readState(ctx.dir);
    expect(firstState.sources["source.md"]?.hash).toBe(pending.oldHash);
    expect((await compileAndReport(ctx.dir)).errors).toEqual([]);
    expect(pending.calls).toHaveLength(2);
    expect(pending.calls[1]).toContain("Edited bytes arrived during the model request.");
  });
});
