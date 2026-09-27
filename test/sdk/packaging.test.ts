/**
 * @file test/sdk/packaging.test.ts
 * @description Install-from-tarball packaging smoke test for the SDK library entry.
 *
 * Validates the full publish contract by running `npm pack`, installing the
 * resulting tarball into a throwaway ESM fixture project, and importing
 * `createWiki` from the installed package via a probe script.
 *
 * This catches mistakes that a local `dist/` import would miss:
 *   - Wrong/missing `exports` map entries
 *   - Files accidentally excluded from the `files` field
 *   - A shebang on the library bundle that would break `import`
 *   - Missing type declarations (`.d.ts`)
 *
 * Network-dependent: `npm install <tarball>` resolves llmwiki's ~17 runtime
 * deps fresh from the registry, so this `test:pack` smoke test is excluded
 * from the default `npm test` run and runs separately in the Linux CI gate.
 * Run explicitly with: `npm run test:pack`
 */

import { describe, it, expect } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { npmInstallEnvironment } from "../fixtures/npm-command.js";

describe("packaging (slow; run via `npm run test:pack`)", () => {
  it("library entry imports from a packed tarball, no shebang, types emitted", async () => {
    const repo = process.cwd();
    execFileSync("npm", ["run", "build"], { stdio: "ignore", cwd: repo });
    expect((await readFile(path.join(repo, "dist/index.js"), "utf-8")).startsWith("#!")).toBe(false);
    await readFile(path.join(repo, "dist/index.d.ts"), "utf-8"); // throws if missing

    const out = await mkdtemp(path.join(tmpdir(), "wiki-pack-"));    // pack INTO temp
    const fixture = await mkdtemp(path.join(tmpdir(), "wiki-fix-"));
    try {
      const packOutput = execFileSync("npm", ["pack", "--silent", "--pack-destination", out], { cwd: repo }).toString();
      // Take the last non-empty line that ends with .tgz to guard against any
      // npm noise that --silent doesn't suppress in all npm versions.
      const name = packOutput
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l.endsWith(".tgz"))
        .at(-1)!;
      expect(name).toMatch(/\.tgz$/);
      await writeFile(path.join(fixture, "package.json"), JSON.stringify({ name: "f", type: "module" }));
      execFileSync("npm", ["install", "--ignore-scripts", path.join(out, name)],
        { cwd: fixture, stdio: "ignore", env: npmInstallEnvironment() });
      await readFile(path.join(fixture, "node_modules/llm-wiki-compiler/dist/index.d.ts"), "utf-8");
      await writeFile(path.join(fixture, "probe.mjs"), `import { createWiki } from "llm-wiki-compiler"; if (typeof createWiki !== "function") process.exit(2);`);
      execFileSync("node", ["probe.mjs"], { cwd: fixture, stdio: "ignore" });
    } finally {
      await rm(out, { recursive: true, force: true });   // no .tgz left in the repo
      await rm(fixture, { recursive: true, force: true });
    }
  }, 180_000);
});
