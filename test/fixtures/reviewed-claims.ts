/** Synthetic immutable claim projections exercise the same sealed read boundary as private replicas. */
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

export const CLAIM_RECORD = "a".repeat(64);
export const CLAIM_REF = `${CLAIM_RECORD}:0`;
export const CLAIM_TEXT = "样例小游戏更新时必须保留玩家存档。";

/** Construct one reviewed claim without publishing a topic page or manufacturing source files. */
export function reviewedClaim(overrides: Record<string, unknown> = {}) {
  return { claimRef: CLAIM_REF, recordId: CLAIM_RECORD, projectId: "sample-game", projectLabel: "Sample Game",
    title: "存档保留", topic: "小游戏存档", decisionObject: "更新与存档", text: CLAIM_TEXT,
    kind: "constraint", status: "decided", useWhen: "仅适用于样例小游戏更新", rationale: "避免玩家进度丢失",
    recordedAt: "2026-09-01T00:00:00Z", targetPageId: null, superseded: false, equivalentPageRefs: [],
    quotes: [{ evidenceId: "user-1", kind: "user", quote: CLAIM_TEXT, locator: "turn:synthetic-1",
      observedAt: "2026-09-01T00:00:00Z", sha256: createHash("sha256").update(CLAIM_TEXT).digest("hex") }],
    ...overrides };
}

/** Seal the exact bytes consumed by TypeScript; test changes must update the seal explicitly. */
export async function writeReviewedClaims(root: string, claims = [reviewedClaim()], superseded: unknown[] = []) {
  await mkdir(path.join(root, ".llmwiki"), { recursive: true });
  const generationId = path.basename(root);
  const bytes = JSON.stringify({ version: 2, generationId, claims, superseded, rejectedRecordIds: [] });
  await writeFile(path.join(root, ".llmwiki/reviewed-claims.json"), bytes);
  await writeFile(path.join(root, ".llmwiki/projection-manifest.json"), JSON.stringify({
    version: 2, generationId, responseSha256: "0".repeat(64), files: [],
    consumerFiles: [{ path: ".llmwiki/reviewed-claims.json", sha256: createHash("sha256").update(bytes).digest("hex") }],
  }));
}
