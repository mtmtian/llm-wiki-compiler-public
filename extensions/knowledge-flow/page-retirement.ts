/**
 * Whole-page retirement is an explicit reviewed migration operation. It names
 * the exact old bytes, project owner and external process record. No model
 * classification or missing generated file grants permission to delete a page.
 */
import { z } from "zod";
import { parseFrontmatter } from "../../src/utils/markdown.js";
import { parseQualifiedPageId } from "../../src/utils/page-id.js";
import { sha256Text } from "../../src/connectors/hash.js";
import { citationMarkers, validRetirementUrl } from "./citation-retirement.js";
import type { CitationRetirement } from "./citation-retirement.js";
import { receiptCoversPage } from "./retirement-receipt.js";
import type { RetirementReceipt } from "./retirement-receipt.js";
import type { FlowConfig } from "./types.js";

export interface TopicPageRetirement {
  projectId: string;
  pageId: string;
  sha256: string;
  reason: string;
  externalReference: string;
}

const text = (maximum: number) => z.string().min(1).max(maximum).refine(value => value === value.trim());
const schema = z.array(z.object({ projectId: text(160), pageId: text(256),
  sha256: z.string().regex(/^[a-f0-9]{64}$/), reason: text(1000), externalReference: text(2048),
}).strict()).max(1000);

/** Revalidate untrusted process-boundary input and disjoint migration ownership. */
export function validatePageRetirements(value: unknown, reserved: Set<string>): TopicPageRetirement[] {
  if (value === undefined) return [];
  const parsed = schema.safeParse(value);
  if (!parsed.success) throw new Error("invalid reviewed page retirements");
  const seen = new Set(reserved);
  for (const item of parsed.data) {
    const id = parseQualifiedPageId(item.pageId);
    if (!id || id.namespace !== "concepts" || id.pagePart.startsWith(".") || id.pagePart.endsWith(".")
      || /[\x00-\x1f\x7f]/.test(item.pageId) || seen.has(item.pageId)) throw new Error("retired page is duplicated or outside concepts");
    if (!validRetirementUrl(item.externalReference)) {
      throw new Error("retired page external reference is invalid");
    }
    seen.add(item.pageId);
  }
  return parsed.data;
}

/** Apply only against frozen bytes and the configured project boundary. */
export function applyPageRetirements(config: FlowConfig, items: TopicPageRetirement[],
  pages: Map<string, string>, receipt?: RetirementReceipt): CitationRetirement[] {
  const removed: CitationRetirement[] = [];
  for (const item of items) {
    const body = pages.get(item.pageId);
    if (body === undefined) {
      if (receipt && receiptCoversPage(receipt, item)) continue;
      throw new Error("retired page basis hash mismatch");
    }
    if (sha256Text(body) !== item.sha256) throw new Error("retired page basis hash mismatch");
    const projectId = parseFrontmatter(body).meta.projectId;
    const owners = Object.entries(config.projects ?? {}).filter(([, project]) => project.pages?.includes(item.pageId)).map(([id]) => id);
    if (typeof projectId === "string" && projectId !== item.projectId || owners.some(id => id !== item.projectId)) {
      throw new Error("retired page crosses project ownership");
    }
    removed.push(...citationMarkers(body).map(citation => ({ citation, reason: item.reason, replacement: item.externalReference })));
    pages.delete(item.pageId);
  }
  return removed;
}
