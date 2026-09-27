/**
 * Reproduce Git's exported hook environment using two disposable repositories.
 * The real pre-push script must let npm's test fixtures create their own Git
 * histories without moving the checkout's HEAD or overwriting its index.
 */
import { execFileSync, spawnSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, expect, it } from "vitest";

const hook = path.resolve(".husky/pre-push");
let root: string;
afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }); });

/** Git writes are restricted to disposable repositories with a test identity. */
function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
    "-c", "commit.gpgsign=false", ...args], { cwd, encoding: "utf8" }).trim();
}

/** Replace only npm; the fixture still uses real Git and the actual hook script. */
async function createRepositories() {
  root = await mkdtemp(path.join(tmpdir(), "llmwiki-pre-push-"));
  const outer = path.join(root, "outer"), inner = path.join(root, "inner"), bin = path.join(root, "bin");
  await Promise.all([outer, inner, bin].map((directory) => mkdir(directory)));
  git(outer, "init", "-q");
  await writeFile(path.join(outer, "outer.txt"), "preserve this checkout");
  git(outer, "add", ".");
  git(outer, "commit", "-qm", "outer");
  await writeFile(path.join(inner, "inner.txt"), "temporary test fixture");
  await writeFile(path.join(bin, "npm"), `#!/bin/sh
set -eu
if [ "$1" = test ]; then
  cd "$INNER_REPOSITORY"
  git init -q
  git add .
  git -c user.name=Test -c user.email=test@example.invalid -c commit.gpgsign=false commit -qm fixture
fi
`, { mode: 0o755 });
  return { outer, inner, bin };
}

it.each([false, true])("isolates fixture history and index with GIT_WORK_TREE=%s", async (withWorkTree) => {
  const { outer, inner, bin } = await createRepositories();
  const head = git(outer, "rev-parse", "HEAD"), tree = git(outer, "write-tree");
  const result = spawnSync("sh", ["-e", hook], { cwd: outer, encoding: "utf8", env: {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    INNER_REPOSITORY: inner, GIT_DIR: path.join(outer, ".git"), GIT_WORK_TREE: withWorkTree ? outer : undefined,
    GIT_INDEX_FILE: path.join(outer, ".git/index"), GIT_PREFIX: "",
  } });
  expect(result.error).toBeUndefined();
  expect(result.status, result.stdout + result.stderr).toBe(0);
  expect(git(outer, "rev-parse", "HEAD")).toBe(head);
  expect(git(outer, "write-tree")).toBe(tree);
  expect(git(inner, "log", "-1", "--format=%s")).toBe("fixture");
  expect(git(inner, "ls-files")).toBe("inner.txt");
});
