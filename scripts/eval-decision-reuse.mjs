#!/usr/bin/env node
/** Run the shared synthetic retrieval baseline through the existing test runner. */
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const outputPath = path.resolve(process.argv[2] ?? path.join(tmpdir(), "llmwiki-decision-reuse-report.json"));
const vitestCli = require.resolve("vitest/vitest.mjs");
const revision = process.env.DECISION_REUSE_SOURCE_REVISION ?? resolveProductionRevision(projectRoot);
const result = spawnSync(process.execPath, [vitestCli, "run", "test/decision-reuse-report.test.ts", "--reporter=default"], {
  cwd: projectRoot,
  env: { ...process.env, DECISION_REUSE_REPORT: outputPath, DECISION_REUSE_SOURCE_REVISION: revision },
  stdio: "inherit",
});

if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
process.stdout.write(`Decision reuse report: ${outputPath}\n`);

/** Distinguish reports from committed production code and local source changes. */
function resolveProductionRevision(root) {
  const commit = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).stdout.trim();
  if (!commit) return "working-tree";
  const productionFiles = ["src", "extensions/knowledge-flow"];
  const changes = spawnSync("git", ["status", "--porcelain", "--", ...productionFiles], {
    cwd: root, encoding: "utf8",
  }).stdout.trim();
  return changes ? `${commit}+production-dirty` : commit;
}
