/**
 * Durable receipt for an applied whole-page retirement.
 *
 * A missing page is idempotent only when this receipt proves that the exact
 * migration input already removed the exact project-owned page bytes. The
 * receipt is intentionally narrow: it records no rendered content and never
 * authorizes a present page whose bytes differ from the reviewed hash.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";
import { z } from "zod";
import { sha256Text } from "../../src/connectors/hash.js";
import { atomicWrite } from "../../src/utils/markdown.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import type { TopicPageRetirement } from "./page-retirement.js";
import type { TopicPage } from "./publication-types.js";

const RECEIPT_PATH = ".llmwiki/page-retirement-receipt.json";
const HASH = /^[a-f0-9]{64}$/;

export interface RetirementReceipt {
  version: 1;
  inputHash: string;
  manifestHash: string;
  retiredPages: Array<Pick<TopicPageRetirement, "projectId" | "pageId" | "sha256"> & { claimRefs: string[] }>;
}

const receiptSchema = z.object({
  version: z.literal(1),
  inputHash: z.string().regex(HASH),
  manifestHash: z.string().regex(HASH),
  retiredPages: z.array(z.object({
    projectId: z.string().min(1).max(160),
    pageId: z.string().min(1).max(256),
    sha256: z.string().regex(HASH),
    claimRefs: z.array(z.string().regex(/^[a-f0-9]{64}:[0-4]$/)).max(5000)
      .refine(refs => new Set(refs).size === refs.length),
  }).strict()).max(1000),
}).strict();

/** Hash the complete migration manifest, including reasons and references. */
function retirementManifestHash(migration: unknown): string {
  return sha256Text(JSON.stringify(migration));
}

/** Read the receipt, failing closed when an existing file is malformed. */
export async function readRetirementReceipt(root: string): Promise<RetirementReceipt | null> {
  const file = await confineUnderRoot(RECEIPT_PATH, root, { mustExist: false });
  try {
    const parsed: unknown = JSON.parse(await readFile(file, "utf8"));
    const result = receiptSchema.safeParse(parsed);
    if (!result.success) throw new Error("invalid page-retirement receipt");
    return result.data;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    if (error instanceof Error && error.message === "invalid page-retirement receipt") throw error;
    throw new Error("invalid page-retirement receipt");
  }
}

/** Match receipt identity and every retired page before allowing missing-page replay. */
export function matchesRetirementReceipt(
  receipt: RetirementReceipt | null,
  inputHash: string,
  migration: { retiredPages?: TopicPageRetirement[] } | undefined,
): receipt is RetirementReceipt {
  const pages = migration?.retiredPages ?? [];
  if (!receipt || !HASH.test(inputHash) || pages.length === 0) return false;
  if (receipt.inputHash !== inputHash || receipt.manifestHash !== retirementManifestHash(migration)) return false;
  return samePageBasis(receipt.retiredPages, pages);
}

/** Return whether a receipt covers one exact project/page/hash tuple. */
export function receiptCoversPage(receipt: RetirementReceipt, page: TopicPageRetirement): boolean {
  return receipt.retiredPages.some(item => item.projectId === page.projectId
    && item.pageId === page.pageId && item.sha256 === page.sha256);
}

/** Bind the actual retired contributions, including claims routed without a target id. */
export function createRetirementReceipt(
  inputHash: string,
  migration: { retiredPages?: TopicPageRetirement[] } | undefined,
  plannedPages: TopicPage[],
): RetirementReceipt | null {
  const pages = migration?.retiredPages ?? [];
  if (!pages.length) return null;
  return {
    version: 1,
    inputHash,
    manifestHash: retirementManifestHash(migration),
    retiredPages: pages.map(({ projectId, pageId, sha256 }) => ({ projectId, pageId, sha256,
      claimRefs: (plannedPages.find(page => page.id === pageId)?.entries ?? []).map(entry => entry.ref).sort(),
    })),
  };
}

/** Persist a receipt only after the complete materialization succeeds. */
export async function writeRetirementReceipt(root: string, receipt: RetirementReceipt | null): Promise<void> {
  if (!receipt) return;
  await confineUnderRoot(RECEIPT_PATH, root, { mustExist: false });
  const file = path.join(root, RECEIPT_PATH);
  await atomicWrite(file, `${JSON.stringify(receipt, null, 2)}\n`, { confineRoot: root });
}

function samePageBasis(
  recorded: RetirementReceipt["retiredPages"],
  expected: TopicPageRetirement[],
): boolean {
  const key = (item: Pick<TopicPageRetirement, "projectId" | "pageId" | "sha256">): string =>
    `${item.projectId}\u0000${item.pageId}\u0000${item.sha256}`;
  const left = recorded.map(key).sort();
  const right = expected.map(key).sort();
  return JSON.stringify(left) === JSON.stringify(right);
}
