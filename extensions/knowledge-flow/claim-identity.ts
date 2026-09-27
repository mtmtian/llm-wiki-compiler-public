/** Deterministic duplicate detection across machines and local job identities. */
import { sha256Text } from "../../src/connectors/hash.js";
import { parseFrontmatter } from "../../src/utils/markdown.js";
import type { FlowClaim } from "./types.js";

/** A changed claim or supporting quote is new evidence, not an automatic overwrite. */
export function claimIdentity(projectId: string, claim: FlowClaim): string {
  const normalize = (value: string) => value.normalize("NFC").trim().replace(/\s+/g, " ");
  return sha256Text(JSON.stringify([projectId, claim.kind, normalize(claim.text), normalize(claim.quote)]));
}

/** Only identities actually retained in accepted pages suppress future writes. */
export function publishedClaimIds(pages: ReadonlyMap<string, string>): Set<string> {
  const ids = new Set<string>();
  for (const body of pages.values()) {
    const values = parseFrontmatter(body).meta.knowledgeClaimIds;
    if (Array.isArray(values)) for (const value of values) {
      if (typeof value === "string" && /^[a-f0-9]{64}$/.test(value)) ids.add(value);
    }
  }
  return ids;
}
