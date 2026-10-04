/** Audit-only queue-full holds can be dismissed without changing their evidence anchor. */
import { afterEach, describe, expect, it } from "vitest";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { resolvePendingReview } from "../extensions/knowledge-flow/resolve.js";

let root = "";
afterEach(async () => {
  if (root) await rm(root, { recursive: true, force: true });
});

/** Create a completed audit-only queue-full batch using the persisted worker schema. */
async function createQueueFullHold(jobId: string): Promise<{ auditPath: string; auditText: string }> {
  const auditPath = path.join(root, "audit", `${jobId}.json`);
  const batchPath = path.join(root, "batches", `${jobId}.json`);
  const auditText = `{"status":"needs_review","error":"review queue is full","jobId":"${jobId}","projectId":"fixture"}\n`;
  await mkdirQueue(root, "audit");
  await mkdirQueue(root, "batches");
  await writeFile(auditPath, auditText, "utf8");
  await writeFile(batchPath, JSON.stringify({ batchId: jobId, status: "completed",
    job: { id: jobId, projectId: "fixture" },
    result: { status: "needs_review", error: "review queue is full" } }), "utf8");
  return { auditPath, auditText };
}

describe("knowledge-flow resolution", () => {
  it("dismisses an audit-only hold idempotently while preserving audit bytes", async () => {
    root = await mkdtemp(path.join(tmpdir(), "flow-resolve-"));
    const { auditPath, auditText } = await createQueueFullHold("batch-full");

    expect(await resolvePendingReview(root, "batch-full", "dismiss")).toBe(true);
    const archivePath = path.join(root, "resolved", "batch-full.json");
    const firstArchive = await readFile(archivePath, "utf8");
    expect(JSON.parse(firstArchive)).toMatchObject({ action: "dismiss", anchor: "audit" });
    expect(await readFile(auditPath, "utf8")).toBe(auditText);
    expect(await resolvePendingReview(root, "batch-full", "dismiss")).toBe(true);
    expect(await readFile(archivePath, "utf8")).toBe(firstArchive);
  });

  it("refuses dismissal while a frozen retry remains queued", async () => {
    root = await mkdtemp(path.join(tmpdir(), "flow-resolve-retry-"));
    const { auditPath, auditText } = await createQueueFullHold("batch-full");
    await mkdirQueue(root, "review-retries");
    await writeFile(path.join(root, "review-retries", "review-retry.json"),
      JSON.stringify({ originalJobId: "batch-full" }), "utf8");
    await mkdirQueue(root, "queue");
    await writeFile(path.join(root, "queue", "review-retry.json"),
      JSON.stringify({ id: "review-retry", reviewRetryOf: "batch-full" }), "utf8");

    await expect(resolvePendingReview(root, "batch-full", "dismiss")).rejects.toThrow("retry in progress");
    expect(await readFile(auditPath, "utf8")).toBe(auditText);
    await expect(readFile(path.join(root, "resolved", "batch-full.json"), "utf8")).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("keeps ordinary review dismissal behavior", async () => {
    root = await mkdtemp(path.join(tmpdir(), "flow-resolve-review-"));
    const reviewPath = path.join(root, "review", "ordinary.json");
    await mkdirQueue(root, "review");
    await writeFile(reviewPath, JSON.stringify({ jobId: "ordinary", decisions: [] }), "utf8");

    expect(await resolvePendingReview(root, "ordinary", "dismiss")).toBe(true);
    expect(JSON.parse(await readFile(path.join(root, "resolved", "ordinary.json"), "utf8")))
      .toMatchObject({ action: "dismiss", review: { jobId: "ordinary" } });
    await expect(readFile(reviewPath, "utf8")).rejects.toMatchObject({ code: "ENOENT" });
  });
});

/** Create one fixture directory without introducing state outside the temp root. */
async function mkdirQueue(rootDir: string, name: string): Promise<void> {
  const { mkdir } = await import("node:fs/promises");
  await mkdir(path.join(rootDir, name), { recursive: true });
}
