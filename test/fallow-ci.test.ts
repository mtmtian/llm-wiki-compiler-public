/**
 * Run the real CI script and pinned analyzer in a tiny project. Fallow's normal
 * exit code allows warning-level findings, so the gate must reject a real dead
 * file and then accept the same project after that file is removed.
 */
import { spawnSync } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, expect, it } from "vitest";
import { npmCommand } from "./fixtures/npm-command.js";

let root: string;
afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }); });

/** Copy the actual npm entry point while keeping all analyzer inputs temporary. */
async function createProject(): Promise<void> {
  root = await mkdtemp(path.join(tmpdir(), "llmwiki-fallow-gate-"));
  const manifest = JSON.parse(await readFile("package.json", "utf8"));
  await mkdir(path.join(root, "scripts"));
  await cp("scripts/fallow-ci.sh", path.join(root, "scripts/fallow-ci.sh"));
  await symlink(path.resolve("node_modules"), path.join(root, "node_modules"), "dir");
  await writeFile(path.join(root, "package.json"), JSON.stringify({ name: "fallow-gate-fixture",
    private: true, scripts: { "fallow:ci": manifest.scripts["fallow:ci"] } }));
  await writeFile(path.join(root, ".fallowrc.json"), JSON.stringify({ entry: ["index.ts"],
    rules: { "unused-files": "warn" }, ignorePatterns: ["scripts/**"] }));
  await writeFile(path.join(root, "index.ts"), 'console.log("used entry");\n');
  await writeFile(path.join(root, "unused.ts"), "export const leftover = 1;\n");
}

/** Use the same npm script as CI without downloading another analyzer version. */
function analyze() {
  return spawnSync(...npmCommand(["run", "fallow:ci"]), { cwd: root, encoding: "utf8", timeout: 30_000 });
}

it("rejects a warning-level dead file and accepts its removal", async () => {
  await createProject();
  const finding = analyze();
  expect(finding.error).toBeUndefined();
  expect(finding.stdout).toContain("unused.ts");
  expect(finding.status).toBe(1);
  await rm(path.join(root, "unused.ts"));
  const clean = analyze();
  expect(clean.error).toBeUndefined();
  expect(clean.status).toBe(0);
});
