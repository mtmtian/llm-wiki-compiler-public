/**
 * Quarantine diagnostics must reach read-only lint and status surfaces, deduplicate
 * interrupted settlements, and leave both durable retry markers unchanged.
 */

import { readFile, symlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { collectStatus } from "../src/status/collect.js";
import { lint } from "../src/linter/index.js";
import {
  PENDING_EMBEDDINGS_FILE,
  QUARANTINED_EMBEDDINGS_FILE,
  MAX_PENDING_EMBEDDING_ATTEMPTS,
} from "../src/utils/constants.js";
import { writePendingEmbeddings } from "../src/utils/pending-embeddings.js";
import { useCompileProject } from "./fixtures/compile-project.js";

const ctx = useCompileProject({ dirSuffix: "quarantine-surfaces" });
const QUARANTINED = "embeddings-refresh-quarantined";
const UNAVAILABLE = "embeddings-quarantine-unavailable";

/** Read the actual status and lint pipelines rather than a stubbed warning mapper. */
async function inspect(): Promise<{ status: Awaited<ReturnType<typeof collectStatus>>; lint: Awaited<ReturnType<typeof lint>> }> {
  return { status: await collectStatus(ctx.dir), lint: await lint(ctx.dir) };
}

/** Plant duplicate stopped state plus one active retry and one exhausted overflow. */
async function seedPending(attempts = MAX_PENDING_EMBEDDING_ATTEMPTS - 1): Promise<void> {
  await writePendingEmbeddings(ctx.dir, [{ pageId: "concepts/stopped", attempts: MAX_PENDING_EMBEDDING_ATTEMPTS }], QUARANTINED_EMBEDDINGS_FILE);
  await writePendingEmbeddings(ctx.dir, [
    { pageId: "concepts/stopped", attempts },
    { pageId: "concepts/overflow", attempts: MAX_PENDING_EMBEDDING_ATTEMPTS },
    { pageId: "concepts/waiting", attempts: 1 },
  ]);
}

it.each([MAX_PENDING_EMBEDDING_ATTEMPTS - 1, MAX_PENDING_EMBEDDING_ATTEMPTS])(
  "counts distinct stopped pages with pending at %i attempts without writing either marker",
  async (attempts) => {
    await seedPending(attempts);
    const files = [PENDING_EMBEDDINGS_FILE, QUARANTINED_EMBEDDINGS_FILE]
      .map((file) => path.join(ctx.dir, file));
    const before = await Promise.all(files.map((file) => readFile(file, "utf8")));

    const result = await inspect();

    expect(result.status.warnings).toContainEqual(expect.objectContaining({
      code: QUARANTINED,
      message: expect.stringContaining("2 page(s)"),
    }));
    expect(result.status.warnings).toContainEqual(expect.objectContaining({
      code: "embeddings-refresh-pending",
      message: expect.stringContaining("1 page(s)"),
    }));
    expect(result.lint.results).toContainEqual(expect.objectContaining({
      rule: "quarantined-embeddings",
      severity: "warning",
      message: expect.stringContaining(QUARANTINED),
    }));
    expect(await Promise.all(files.map((file) => readFile(file, "utf8")))).toEqual(before);
  },
);

it("keeps clean projects free of quarantine warnings", async () => {
  const result = await inspect();
  expect(result.status.warnings).toBeUndefined();
  expect(result.lint.results.some((finding) => finding.rule === "quarantined-embeddings")).toBe(false);
});

it.each(["{bad", "{}"])("reports unreadable quarantine data through lint and status: %s", async (body) => {
  await writeFile(path.join(ctx.dir, QUARANTINED_EMBEDDINGS_FILE), body);
  const result = await inspect();
  expect(result.status.warnings?.map((warning) => warning.code)).toContain(UNAVAILABLE);
  expect(result.lint.results).toContainEqual(expect.objectContaining({
    rule: "quarantined-embeddings",
    file: QUARANTINED_EMBEDDINGS_FILE,
    message: expect.stringContaining(UNAVAILABLE),
  }));
});

it("does not follow a quarantine symlink or change its target", async () => {
  const target = path.join(ctx.dir, "victim.json");
  await writeFile(target, "[]");
  await symlink(target, path.join(ctx.dir, QUARANTINED_EMBEDDINGS_FILE));
  expect((await inspect()).status.warnings?.map((warning) => warning.code)).toContain(UNAVAILABLE);
  expect(await readFile(target, "utf8")).toBe("[]");
});
