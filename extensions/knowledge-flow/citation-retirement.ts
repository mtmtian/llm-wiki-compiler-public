/**
 * Explicit evidence retirement for reviewed page edits. Ordinary edits retain
 * all evidence. Curation names every removed marker and the surviving evidence
 * or external process record, so replay cannot silently discard provenance.
 * Semantic usefulness and external-source support remain independent-review duties.
 */
import { z } from "zod";
import { isIP } from "node:net";
import { parseFrontmatter } from "../../src/utils/markdown.js";

export interface CitationRetirement {
  citation: string;
  reason: string;
  replacement: string;
}

const markerPattern = /^\^\[[^\]\r\n]+\]$/;
const placeholderPattern = /^\{\{claim:[0-4]\}\}$/;
const retirementSchema = z.array(z.object({
  citation: z.string().max(1024).regex(markerPattern),
  reason: z.string().min(1).max(1000).refine(value => value === value.trim()),
  replacement: z.string().min(1).max(2048),
}).strict()).max(500);

/** Extract exact markers from prose, never frontmatter metadata. */
export function citationMarkers(text: string): string[] {
  return [...new Set(parseFrontmatter(text).body.match(/\^\[[^\]\r\n]+\]/g) ?? [])];
}

/** Keep the structural wire contract identical to Python's strict validator. */
export function validateRetirementShape(value: unknown, body: string, claimIndexes: number[] = []): CitationRetirement[] {
  if (value === undefined) return [];
  const parsed = retirementSchema.safeParse(value);
  if (!parsed.success) throw new Error("invalid citation retirements");
  const seen = new Set<string>();
  for (const item of parsed.data) {
    if (seen.has(item.citation) || body.includes(item.citation)) throw new Error("retired citation is duplicated or still present");
    if (!validReplacement(item.replacement, claimIndexes)) {
      throw new Error(`retired citation ${item.citation} has invalid replacement literal ${JSON.stringify(item.replacement)}; use one exact citation, one {{claim:N}} marker in this page, or one HTTPS URL supported by supplied evidence`);
    }
    if (!body.includes(item.replacement)) {
      throw new Error(`retired citation ${item.citation} replacement literal ${JSON.stringify(item.replacement)} is not present in revised body`);
    }
    seen.add(item.citation);
  }
  return parsed.data;
}

function validReplacement(value: string, claimIndexes: number[]): boolean {
  if (markerPattern.test(value)) return true;
  if (placeholderPattern.test(value)) return claimIndexes.includes(Number(value.slice(8, -2)));
  return validRetirementUrl(value);
}

/** External references use HTTPS and an unambiguous DNS/IP host, never userinfo. */
export function validRetirementUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const authority = /^https:\/\/([^/?#]+)/.exec(value)?.[1];
    if (!authority || /[@%\s]/.test(authority) || authority.endsWith(":") || /[\\\x00-\x20\x7f]|\s/.test(value) || value.length > 2048) return false;
    const hostname = url.hostname.replace(/^\[|\]$/g, "");
    const validHost = isIP(hostname) !== 0 || hostname.length <= 253 && hostname.replace(/\.$/, "").split(".")
      .every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label));
    return url.protocol === "https:" && validHost;
  } catch { return false; }
}

/** Reject silent loss, invented old citations, and retirement of unrelated evidence. */
export function validateCitationChanges(previous: string[], body: string, value?: CitationRetirement[],
  options: { newMarkers?: string[]; claimIndexes?: number[] } = {}): void {
  const retirements = validateRetirementShape(value, body, options.claimIndexes);
  const before = new Set(previous.flatMap(citationMarkers));
  const after = new Set(citationMarkers(body));
  const retired = new Set(retirements.map(item => item.citation));
  for (const item of retirements) if (!before.has(item.citation)) throw new Error("retired citation is outside the reviewed basis");
  for (const marker of before) if (!after.has(marker) && !retired.has(marker)) throw new Error("topic edit dropped existing evidence citation without reviewed retirement");
  for (const marker of after) if (!before.has(marker) && !options.newMarkers?.includes(marker)) throw new Error("topic edit invented an existing evidence citation");
}

/** Model-proposed external references must be present in actual supplied evidence. */
export function validateRetirementReferences(retirements: CitationRetirement[], originalEvidence: string[]): void {
  for (const item of retirements) {
    if (item.replacement.startsWith("https:") && !originalEvidence.some(text => text.includes(item.replacement))) {
      throw new Error("retirement external reference is not supported by original evidence");
    }
  }
}
