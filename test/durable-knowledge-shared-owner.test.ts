/**
 * Shared-owner safety regression for explicit empty extraction.
 *
 * When one contributor says no durable knowledge remains, the old page stays
 * live until review. The other contributor still gets re-extracted, but the
 * shared slug must enter the existing frozen set so that run cannot overwrite
 * the page or advance the reviewed source's hash.
 */

import { describe, expect, it, vi } from "vitest";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { AnthropicProvider } from "../src/providers/anthropic.js";
import { compileAndReport } from "../src/compiler/index.js";
import { readState } from "../src/utils/state.js";
import { useCompileProject } from "./fixtures/compile-project.js";

const SHARED_CONCEPT = JSON.stringify({
  concepts: [{
    concept: "Shared Topic",
    summary: "Facts contributed by both sources.",
    is_new: true,
  }],
});

const EMPTY_SUCCESS = JSON.stringify({
  disposition: "no-durable-knowledge",
  reason: "Only a completed status update remains.",
  concepts: [],
});

const SOURCE_A = "a.md";
const SOURCE_B = "b.md";
const SHARED_SLUG = "shared-topic";

/** Route extraction by source content and switch source A to explicit empty. */
function setupSharedProvider() {
  let needsReview = false;
  const toolCall = vi.spyOn(AnthropicProvider.prototype, "toolCall").mockImplementation(
    async (system: string) => {
      if (needsReview && system.includes("A changed transient status")) return EMPTY_SUCCESS;
      return SHARED_CONCEPT;
    },
  );
  const complete = vi.spyOn(AnthropicProvider.prototype, "complete")
    .mockResolvedValue("Stable shared page body. ^[a.md, b.md]");
  return { toolCall, complete, setNeedsReview: () => { needsReview = true; } };
}

describe("explicit empty extraction with shared ownership", () => {
  const ctx = useCompileProject({
    dirSuffix: "durable-shared-owner",
    sourceFile: SOURCE_A,
    sourceContent: "# A\n\nA original contribution.",
  });

  it("freezes the shared page and preserves the empty owner's state for review", async () => {
    await writeFile(path.join(ctx.dir, "sources", SOURCE_B), "# B\n\nB contribution.", "utf-8");
    const provider = setupSharedProvider();
    vi.spyOn(console, "log").mockImplementation(() => {});

    const first = await compileAndReport(ctx.dir);
    expect(first.errors).toEqual([]);
    const beforeState = await readState(ctx.dir);
    const beforeOwner = { ...beforeState.sources[SOURCE_A] };
    const pagePath = path.join(ctx.dir, "wiki", "concepts", `${SHARED_SLUG}.md`);
    const beforePage = await readFile(pagePath, "utf-8");
    const firstCompleteCalls = provider.complete.mock.calls.length;

    provider.setNeedsReview();
    await writeFile(path.join(ctx.dir, "sources", SOURCE_A), "# A changed transient status\n\nOnly a completed update remains.", "utf-8");

    const second = await compileAndReport(ctx.dir);
    const afterState = await readState(ctx.dir);
    expect(second.errors).toContain(
      "a.md: no durable knowledge — needs review; retaining existing wiki ownership",
    );
    expect(afterState.sources[SOURCE_A]).toEqual(beforeOwner);
    expect(afterState.sources[SOURCE_A]?.hash).not.toBe("");
    expect(afterState.sources[SOURCE_B]?.concepts).toEqual([SHARED_SLUG]);
    expect(afterState.frozenSlugs).toContain(SHARED_SLUG);
    expect(await readFile(pagePath, "utf-8")).toBe(beforePage);
    expect(provider.toolCall.mock.calls.length).toBe(4);
    expect(provider.complete.mock.calls.length).toBe(firstCompleteCalls);
  });
});
