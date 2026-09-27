/**
 * Regression coverage for context-pack page scoping.
 *
 * The scope is an explicit qualified-page allowlist supplied by the caller;
 * it is deliberately narrower than a general search ACL and only affects the
 * context-pack surfaces.
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { buildContextPack } from "../src/context/build.js";
import { createWiki } from "../src/sdk/wiki.js";

let root: string;

beforeEach(async () => {
  root = await mkdtemp(path.join(os.tmpdir(), "llmwiki-context-scope-"));
});

afterEach(async () => {
  await rm(root, { recursive: true, force: true });
});

async function writePage(directory: string, slug: string, title: string, body = ""): Promise<void> {
  const dir = path.join(root, "wiki", directory);
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, `${slug}.md`), `---\ntitle: ${title}\n---\n\n${body}\n`, "utf8");
}

async function writeSource(name: string, text: string): Promise<void> {
  await mkdir(path.join(root, "sources"), { recursive: true });
  await writeFile(path.join(root, "sources", name), text, "utf8");
}

describe("buildContextPack allowedPageIds", () => {
  it("limits ranking across namespaces to the qualified allowlist", async () => {
    await writePage("concepts", "alpha", "Alpha", "shared");
    await writePage("queries", "beta", "Beta", "shared");
    const scope = ["concepts/alpha"];
    const pack = await buildContextPack({ root, prompt: "shared", allowedPageIds: scope });
    expect(pack.primary.map((page) => page.id)).toEqual(["concepts/alpha"]);
    expect(pack.project.pages).toBe(1);
  });

  it("keeps graph expansion inside the same allowlist", async () => {
    await writePage("concepts", "alpha", "Alpha", "[[Gamma]] [[Beta]]");
    await writePage("concepts", "gamma", "Gamma");
    await writePage("concepts", "beta", "Beta");
    const pack = await buildContextPack({
      root,
      prompt: "alpha",
      allowedPageIds: ["concepts/alpha", "concepts/gamma"],
    });
    expect(pack.primary.map((page) => page.id)).toEqual(["concepts/alpha"]);
    expect(pack.neighbors.every((edge) =>
      edge.from === "concepts/alpha" && edge.to === "concepts/gamma",
    )).toBe(true);
    expect(pack.neighbors.some((edge) => edge.to === "concepts/beta")).toBe(false);
    expect(pack.gaps).toEqual([]);
  });

  it("only materializes source windows for an allowed primary page", async () => {
    await writePage("concepts", "alpha", "Alpha", "Claim A shared ^[alpha.md:1-1]");
    await writePage("concepts", "beta", "Beta", "Claim B shared ^[beta.md:1-1]");
    await writeSource("alpha.md", "alpha evidence");
    await writeSource("beta.md", "beta evidence");
    const pack = await buildContextPack({
      root,
      prompt: "shared",
      includeSources: true,
      allowedPageIds: ["concepts/alpha"],
    });
    expect(pack.primary).toHaveLength(1);
    expect(pack.primary[0].sourceWindows.map((window) => window.file)).toEqual(["alpha.md"]);
  });

  it("returns empty page-bearing surfaces for an explicit empty scope", async () => {
    await writePage("concepts", "alpha", "Alpha", "[[Missing]]");
    const pack = await buildContextPack({ root, prompt: "alpha", allowedPageIds: [] });
    expect(pack.project.pages).toBe(0);
    expect({ primary: pack.primary, neighbors: pack.neighbors, gaps: pack.gaps }).toEqual({
      primary: [],
      neighbors: [],
      gaps: [],
    });
  });

  it("preserves the unrestricted page pool when no scope is supplied", async () => {
    await writePage("concepts", "alpha", "Alpha", "shared");
    await writePage("concepts", "beta", "Beta", "shared");
    const pack = await buildContextPack({ root, prompt: "shared" });
    expect(pack.primary).toHaveLength(2);
    expect(pack.primary.map((page) => page.id).sort()).toEqual(["concepts/alpha", "concepts/beta"]);
  });

  it("forwards the page scope through the SDK facade", async () => {
    await writePage("concepts", "alpha", "Alpha", "shared");
    await writePage("queries", "beta", "Beta", "shared");
    const pack = await createWiki({ root }).getContextPack({
      prompt: "shared",
      allowedPageIds: ["concepts/alpha"],
    });
    expect(pack.primary.map((page) => page.id)).toEqual(["concepts/alpha"]);
  });
});
