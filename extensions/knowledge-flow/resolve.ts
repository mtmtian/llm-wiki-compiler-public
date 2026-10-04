/**
 * Explicitly dismisses a pending knowledge-flow review item.
 *
 * Review files keep their existing archive-and-remove behavior. A queue-full
 * hold has no review file, so dismissal records a durable disposition beside
 * its immutable audit anchor. No action here accepts a candidate or rewrites
 * the source evidence.
 */

import { createHash } from "node:crypto";
import { access, mkdir, readFile, readdir, unlink } from "node:fs/promises";
import path from "node:path";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { atomicWrite } from "../../src/utils/markdown.js";

const QUEUE_FULL_ERROR = "review queue is full";
const ACTIVE_BATCH_STATUSES = new Set(["claimed", "retry", "result-ready", "finalize-retry", "sync-retry"]);

/**
 * Remove one pending item or persist dismissal of its audit-only queue-full hold.
 * Concurrent callers must hold the state directory's worker.lock, as maintenance does.
 */
export async function resolvePendingReview(
  stateDir: string,
  jobId: string,
  action: "reject" | "dismiss",
): Promise<boolean> {
  if (action !== "reject" && action !== "dismiss") throw new Error("pending reviews only support reject or dismiss");
  const safeId = jobId.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 120);
  if (!jobId || jobId.includes("\0") || !safeId) throw new Error("job id is not a safe review identifier");
  const file = await confineUnderRoot(path.join("review", `${safeId}.json`), stateDir, { mustExist: false });
  try {
    const review = await readFile(file, "utf8");
    const archive = path.join(stateDir, "resolved", `${safeId}.json`);
    await mkdir(path.dirname(archive), { recursive: true });
    await atomicWrite(archive, JSON.stringify({ action, resolvedAt: new Date().toISOString(), review: JSON.parse(review) }, null, 2), { confineRoot: stateDir });
    await unlink(file);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  return action === "dismiss" ? dismissQueueFullHold(stateDir, safeId) : false;
}

/** Persist an audit disposition only when the batch confirms the exact queue-full hold. */
async function dismissQueueFullHold(stateDir: string, jobId: string): Promise<boolean> {
  const auditText = await readStateText(stateDir, path.join("audit", `${jobId}.json`));
  const batch = await readStateObject(stateDir, path.join("batches", `${jobId}.json`));
  if (auditText === null || !isQueueFullHold(auditText, batch, jobId)) return false;
  const auditHash = createHash("sha256").update(auditText).digest("hex");
  const resolvedPath = path.join("resolved", `${jobId}.json`);
  const prior = await readStateObject(stateDir, resolvedPath);
  if (prior !== null) {
    if (prior.action === "dismiss" && prior.anchor === "audit" && prior.auditHash === auditHash) return true;
    throw new Error("queue-full hold already has a different resolution");
  }
  if (await retryIsInFlight(stateDir, jobId)) throw new Error("queue-full hold has a retry in progress");
  const archive = path.join(stateDir, resolvedPath);
  await mkdir(path.dirname(archive), { recursive: true });
  await atomicWrite(archive, JSON.stringify({ action: "dismiss", anchor: "audit",
    resolvedAt: new Date().toISOString(), auditHash }, null, 2), { confineRoot: stateDir });
  return true;
}

/** Validate both durable records before treating an audit as a dismissible hold. */
function isQueueFullHold(auditText: string, batch: Record<string, unknown> | null, jobId: string): boolean {
  const audit = readQueueFullAudit(auditText, jobId);
  return audit !== null && matchesQueueFullBatch(batch, jobId, audit.projectId);
}

/** Parse an audit only when it names this exact queue-full refusal. */
function readQueueFullAudit(auditText: string, jobId: string): Record<string, unknown> | null {
  let value: unknown;
  try {
    value = JSON.parse(auditText) as unknown;
  } catch {
    return null;
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const audit = value as Record<string, unknown>;
  return audit.status === "needs_review" && audit.error === QUEUE_FULL_ERROR && audit.jobId === jobId ? audit : null;
}

/** Require the completed batch result to agree with the immutable audit identity. */
function matchesQueueFullBatch(
  batch: Record<string, unknown> | null,
  jobId: string,
  projectId: unknown,
): boolean {
  return matchesBatchIdentity(batch, jobId, projectId) && hasQueueFullResult(batch);
}

/** Require the batch and embedded job to agree with the audit identity. */
function matchesBatchIdentity(
  batch: Record<string, unknown> | null,
  jobId: string,
  projectId: unknown,
): boolean {
  const job = batch?.job as Record<string, unknown> | undefined;
  return batch?.batchId === jobId && job?.id === jobId && projectId === job?.projectId;
}

/** Require a completed batch result to record the queue-full review state. */
function hasQueueFullResult(batch: Record<string, unknown> | null): boolean {
  const result = batch?.result as Record<string, unknown> | undefined;
  return batch?.status === "completed" && result?.status === "needs_review"
    && result.error === QUEUE_FULL_ERROR;
}

/** Stop dismissal while a frozen retry is queued or awaiting durable finalization. */
async function retryIsInFlight(stateDir: string, originalId: string): Promise<boolean> {
  const directory = path.join(stateDir, "review-retries");
  let entries: string[];
  try {
    entries = await readdir(directory);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
  for (const entry of entries.filter((name) => name.endsWith(".json"))) {
    const manifest = await readStateObject(stateDir, path.join("review-retries", entry));
    if (manifest?.originalJobId !== originalId) continue;
    const retryId = entry.slice(0, -5);
    if (await stateFileExists(stateDir, path.join("queue", `${retryId}.json`))) return true;
    const batch = await readStateObject(stateDir, path.join("batches", `${retryId}.json`));
    if (batch && ACTIVE_BATCH_STATUSES.has(String(batch.status))) return true;
  }
  return false;
}

/** Read one confined state file, returning null only when it is absent. */
async function readStateText(stateDir: string, relative: string): Promise<string | null> {
  try {
    const candidate = await confineUnderRoot(relative, stateDir, { mustExist: false });
    await access(candidate);
    const file = await confineUnderRoot(relative, stateDir, { mustExist: true });
    return await readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
}

/** Parse one confined state object without accepting malformed records. */
async function readStateObject(stateDir: string, relative: string): Promise<Record<string, unknown> | null> {
  const text = await readStateText(stateDir, relative);
  if (text === null) return null;
  const value: unknown = JSON.parse(text);
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

/** Check a confined file path without creating its parent or following an escaping link. */
async function stateFileExists(stateDir: string, relative: string): Promise<boolean> {
  try {
    const candidate = await confineUnderRoot(relative, stateDir, { mustExist: false });
    await access(candidate);
    await confineUnderRoot(relative, stateDir, { mustExist: true });
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}
