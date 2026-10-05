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

/**
 * The shape the shared contract permits for a claim whose primary source moved to `role`.
 * Assistant text can only carry a historical lesson; artifact text only historical material that is not a decision.
 * A user source permits every shape, so the claim is returned unchanged.
 */
export function roleShapedClaim<T extends Pick<FlowClaim, "kind" | "status">>(claim: T, role: FlowEvidence["kind"]): T {
  if (!authorityViolation(claim, role)) return claim;
  if (role === "assistant") return { ...claim, kind: "lesson", status: "historical" };
  if (role === "artifact") return { ...claim, kind: claim.kind === "decision" ? "fact" : claim.kind, status: "historical" };
  return claim;
}
