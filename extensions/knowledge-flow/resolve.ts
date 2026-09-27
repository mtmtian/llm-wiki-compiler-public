/**
 * Explicitly dismisses a pending knowledge-flow review item.
 *
 * There is intentionally no accept action here: accepting a conflict would
 * bypass the evidence-bound extractor/reviewer. After the operator decides,
 * a corrected rule goes through the normal compiler candidate/approval path;
 * this endpoint only archives and dismisses the pending item.
 */

import { mkdir, readFile, unlink } from "node:fs/promises";
import path from "node:path";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { atomicWrite } from "../../src/utils/markdown.js";

/** Remove one pending conflict after the operator has rejected or dismissed it. */
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
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}
