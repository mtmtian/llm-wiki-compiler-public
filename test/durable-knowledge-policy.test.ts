/**
 * Durable-knowledge policy and explicit-empty extraction integration tests.
 *
 * These tests exercise the compiler's observable state transitions: an
 * explicit empty decision advances the source hash without producing pages,
 * while legacy empty output remains retryable and protects the last page.
 */

import { describe, expect, it, vi } from "vitest";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { AnthropicProvider } from "../src/providers/anthropic.js";
import { compileAndReport } from "../src/compiler/index.js";
import {
  buildExtractionPrompt,
  buildPagePrompt,
  buildSeedPagePrompt,
  parseConceptExtraction,
} from "../src/compiler/prompts.js";
import { buildRuleExtractionPrompt } from "../src/compiler/rule-prompts.js";
import { DURABLE_KNOWLEDGE_POLICY } from "../src/compiler/knowledge-policy.js";
import { buildFrontmatter } from "../src/utils/markdown.js";
import { readState } from "../src/utils/state.js";
import { CONCEPTS_DIR } from "../src/utils/constants.js";
import type { PageKindRule, SeedPage } from "../src/schema/index.js";
import { useCompileProject } from "./fixtures/compile-project.js";

const EMPTY_SUCCESS = JSON.stringify({
  disposition: "no-durable-knowledge",
  reason: "Only completed coordination updates remain.",
  concepts: [],
});

/** Stub the external extractor and capture its user-visible status output. */
function mockExtraction(response: string) {
  const toolCall = vi.spyOn(AnthropicProvider.prototype, "toolCall").mockResolvedValue(response);
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  return { toolCall, log };
}

/** Seed the old-topic page and source ownership used by owner-safety cases. */
async function seedOwnedTopic(root: string, body: string, hash: string): Promise<string> {
  const pagePath = path.join(root, CONCEPTS_DIR, "old-topic.md");
  const frontmatter = buildFrontmatter({
    title: "Old Topic",
    summary: "Existing page",
    sources: ["source.md"],
    createdAt: "2026-01-01T00:00:00.000Z",
    updatedAt: "2026-01-01T00:00:00.000Z",
  });
  await writeFile(pagePath, `${frontmatter}\n\n${body}\n`, "utf-8");
  await writeFile(
    path.join(root, ".llmwiki", "state.json"),
    JSON.stringify({
      version: 1,
      indexHash: "",
      sources: {
        "source.md": {
          hash,
          concepts: ["old-topic"],
          compiledAt: "2026-01-01T00:00:00.000Z",
        },
      },
    }),
    "utf-8",
  );
  return pagePath;
}

describe("shared durable-knowledge policy", () => {
  it("is present in every ordinary generation prompt", () => {
    const seed: SeedPage = {
      title: "Overview",
      kind: "overview",
      summary: "A summary",
      relatedSlugs: [],
    };
    const rule: PageKindRule = { description: "An overview", minWikilinks: 0 };
    const prompts = [
      buildExtractionPrompt("source", ""),
      buildPagePrompt("Concept", "source", "", ""),
      buildSeedPagePrompt(seed, rule, "related"),
      buildRuleExtractionPrompt("source"),
    ];
    for (const prompt of prompts) expect(prompt).toContain(DURABLE_KNOWLEDGE_POLICY);
  });

  it("accepts only an explicit empty disposition with a reason", () => {
    expect(parseConceptExtraction(EMPTY_SUCCESS)).toMatchObject({
      disposition: "no-durable-knowledge",
      concepts: [],
      reason: expect.stringContaining("completed"),
    });
    expect(parseConceptExtraction(JSON.stringify({ concepts: [] })).disposition).toBe("invalid");
    expect(parseConceptExtraction(JSON.stringify({ disposition: "no-durable-knowledge", reason: "Completed", concepts: null })).disposition).toBe("invalid");
    expect(parseConceptExtraction(JSON.stringify({ disposition: "no-durable-knowledge", reason: "Completed" })).disposition).toBe("invalid");
    expect(parseConceptExtraction("not json").disposition).toBe("invalid");
  });
});

describe("explicit empty extraction state", () => {
  const ctx = useCompileProject({
    dirSuffix: "durable-empty",
    sourceFile: "history.md",
    sourceContent: "# Finished handoff\n\nOnly transient status remains.",
  });

  it("records a new source as empty and skips extraction on the next compile", async () => {
    const { toolCall } = mockExtraction(EMPTY_SUCCESS);

    const first = await compileAndReport(ctx.dir);
    const state = await readState(ctx.dir);
    expect(first.errors).toEqual([]);
    expect(state.sources["history.md"]).toMatchObject({ concepts: [] });
    expect(state.sources["history.md"]?.hash).toMatch(/^[0-9a-f]{64}$/);

    const callsAfterFirst = toolCall.mock.calls.length;
    const second = await compileAndReport(ctx.dir);
    expect(toolCall.mock.calls.length).toBe(callsAfterFirst);
    expect(second).toMatchObject({ compiled: 0, skipped: 1, errors: [] });
  });
});

describe("empty extraction safety", () => {
  const ctx = useCompileProject({
    dirSuffix: "durable-empty-owner",
    sourceFile: "source.md",
    sourceContent: "# Updated source\n\nNo durable claim remains.",
  });

  it("keeps an owned page and reports review instead of deleting it", async () => {
    const pagePath = await seedOwnedTopic(ctx.dir, "Existing durable page.", "old-hash");
    const { toolCall, log } = mockExtraction(EMPTY_SUCCESS);

    const result = await compileAndReport(ctx.dir);
    const state = await readState(ctx.dir);
    expect(result.errors.some((error) => error.includes("source.md: no durable knowledge — needs review"))).toBe(true);
    expect(await readFile(pagePath, "utf-8")).toContain("Existing durable page.");
    expect(state.sources["source.md"]?.concepts).toEqual(["old-topic"]);
    expect(state.sources["source.md"]?.hash).toBe("old-hash");
    expect(state.sources["source.md"]?.compiledAt).toBe("2026-01-01T00:00:00.000Z");
    expect(log.mock.calls.flat().join(" ")).toMatch(/needs review/i);

    const second = await compileAndReport(ctx.dir);
    expect(toolCall.mock.calls.length).toBe(2);
    expect(second.errors.some((error) => error.includes("source.md: no durable knowledge — needs review"))).toBe(true);
  });

  it("treats legacy empty output as failure and retains retry state", async () => {
    const pagePath = await seedOwnedTopic(ctx.dir, "Keep this page.", "old-hash-2");
    const { toolCall } = mockExtraction(JSON.stringify({ concepts: [] }));

    const first = await compileAndReport(ctx.dir);
    const state = await readState(ctx.dir);
    expect(first.errors).toContain("No concepts extracted from source.md");
    expect(state.sources["source.md"]?.hash).toBe("");
    expect(state.sources["source.md"]?.concepts).toEqual(["old-topic"]);
    expect(await readFile(pagePath, "utf-8")).toContain("Keep this page.");

    await compileAndReport(ctx.dir);
    expect(toolCall.mock.calls.length).toBe(2);
  });
});
