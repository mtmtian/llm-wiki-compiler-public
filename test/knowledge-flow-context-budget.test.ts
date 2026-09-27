/**
 * Regression coverage for the knowledge-flow hook's context-pack budget.
 *
 * A hook needs the pack envelope and citation windows long enough to select a
 * sourced excerpt. The final hook payload has its own character cap, so the
 * internal pack must keep the default retrieval budget instead of trimming
 * provenance before the hook can read it.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { rm } from "node:fs/promises";
import path from "node:path";
import { makeTempRoot } from "./fixtures/temp-root.js";
import { sha256Hex, writeSourceFile, writeSourceState } from "./fixtures/state-json.js";
import { writePage } from "./fixtures/write-page.js";
import { buildContextPack } from "../src/context/build.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";

const ALLOWED_PAGE_IDS = ["concepts/alpha", "concepts/beta", "concepts/gamma"];
const PAGE_SLUGS = ["alpha", "beta", "gamma"];
const EVIDENCE_PER_PAGE = 6;
const EVIDENCE_LENGTH = 560;
let root = "";

beforeEach(async () => {
  root = await makeTempRoot("knowledge-flow-context-budget");
  await seedProject(root);
});

afterEach(async () => {
  await rm(root, { recursive: true, force: true });
});

async function seedProject(projectRoot: string): Promise<void> {
  const state: Record<string, { hash: string; concepts: string[] }> = {};
  for (const slug of PAGE_SLUGS) {
    const citations: string[] = [];
    for (let index = 1; index <= EVIDENCE_PER_PAGE; index += 1) {
      const filename = `${slug}-${index}.md`;
      const text = `Evidence ${slug}-${index}: ${"x".repeat(EVIDENCE_LENGTH)}\n`;
      await writeSourceFile(projectRoot, filename, text);
      state[filename] = { hash: sha256Hex(text), concepts: [slug] };
      citations.push(`Claim ${slug} ${index} ^[${filename}:1-1]`);
    }
    await writePage(
      path.join(projectRoot, "wiki/concepts"),
      slug,
      { title: `Decision ${slug}`, summary: `Decision evidence for ${slug}` },
      citations.join("\n"),
    );
  }
  await writePage(
    path.join(projectRoot, "wiki/concepts"),
    "outside",
    { title: "Decision outside", summary: "Out of scope decision" },
    "Decision outside context",
  );
  await writeSourceState(projectRoot, state);
}

function sourceWindowCount(pack: Awaited<ReturnType<typeof buildContextPack>>): number {
  return pack.primary.reduce((count, page) => count + page.sourceWindows.length, 0);
}

describe("knowledge-flow hook context budget", () => {
  it("keeps sourced scoped pages after the old 1800-token budget would trim them", async () => {
    const oldPack = await buildContextPack({
      root,
      prompt: "Decision",
      allowedPageIds: ALLOWED_PAGE_IDS,
      budget: 1800,
      topPages: 3,
      depth: 0,
      neighbors: false,
      includeSources: true,
    });
    expect(oldPack.project.pages).toBe(3);
    expect(sourceWindowCount(oldPack)).toBeLessThan(PAGE_SLUGS.length * EVIDENCE_PER_PAGE);

    const result = await buildHookContext({
      config: { wikiRoot: root, maxContextChars: 2400 },
      prompt: "Decision",
      allowedPageIds: ALLOWED_PAGE_IDS,
      seen: {},
    });
    expect(result.context).not.toBe("");
    expect(result.context.length).toBeLessThanOrEqual(2400);
    for (const slug of PAGE_SLUGS) expect(result.context).toContain(`${slug}-1.md`);
    expect(result.context).not.toContain("outside");

    const pack = await buildContextPack({
      root,
      prompt: "Decision",
      allowedPageIds: ALLOWED_PAGE_IDS,
      topPages: 3,
      depth: 0,
      neighbors: false,
      includeSources: true,
    });
    expect(pack.project.pages).toBe(3);
    expect(pack.primary.map((page) => page.id).sort()).toEqual([...ALLOWED_PAGE_IDS].sort());
    expect(pack.primary.every((page) => page.sourceWindows.length > 0)).toBe(true);
  });
});
