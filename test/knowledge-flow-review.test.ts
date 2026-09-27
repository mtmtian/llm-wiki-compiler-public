/** Explicit conflict reports must override a model's optimistic acceptance. */
import { describe, expect, it } from "vitest";
import { reviewClaims } from "../extensions/knowledge-flow/review.js";
import type { FlowClaim, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";

const job = { projectId: "fixture", projectLabel: "Fixture", evidence: [], allowedPageIds: ["concepts/prior"] } as unknown as FlowJob;
const claims = [{ text: "Opposite rule" }] as FlowClaim[];

describe("knowledge review conflict gate", () => {
  it("forces review when an accepted decision reports a conflict", async () => {
    const provider = { toolCall: async () => JSON.stringify({ decisions: [{ index: 0, decision: "accept",
      reason: "new request", conflictingPageIds: ["concepts/prior"] }] }) } as unknown as LLMProvider;
    const result = await reviewClaims(provider, job, claims, new Map([["concepts/prior", "Original rule"]]));
    expect(result[0].decision).toBe("needs_review");
  });

  it("rejects a reported conflict outside the provided scope", async () => {
    const provider = { toolCall: async () => JSON.stringify({ decisions: [{ index: 0, decision: "accept",
      reason: "unknown", conflictingPageIds: ["concepts/foreign"] }] }) } as unknown as LLMProvider;
    await expect(reviewClaims(provider, job, claims, new Map())).rejects.toThrow("out-of-scope");
  });
});
