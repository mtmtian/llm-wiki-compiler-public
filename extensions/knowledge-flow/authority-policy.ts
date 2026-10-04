/** Shared evidence-role checks for draft and correction claim validation. */
import type { FlowClaim, FlowEvidence } from "./types.js";

export type AuthorityViolation = "assistant" | "artifact" | "decided";

/** Return the first authority rule violated by a claim and its primary evidence role. */
export function authorityViolation(claim: Pick<FlowClaim, "kind" | "status">,
  role: FlowEvidence["kind"] | undefined): AuthorityViolation | undefined {
  if (role === "assistant" && (claim.kind !== "lesson" || claim.status !== "historical")) return "assistant";
  if (role === "artifact" && (claim.kind === "decision" || claim.status !== "historical")) return "artifact";
  if (claim.status === "decided" && role !== "user") return "decided";
  return undefined;
}
