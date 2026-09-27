/**
 * Five-claim contracts across model schemas and runtime normalization.
 * The sixth claim must fail closed, and review must still gate the fifth.
 */
import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { extractClaims } from "../extensions/knowledge-flow/extract.js";
import { reviewClaims } from "../extensions/knowledge-flow/review.js";
import type { FlowClaim, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";

const evidenceText = "保留五个有价值且独立的项目决策";
const job = { projectId: "fixture", projectLabel: "Fixture", prompt: evidenceText, lastAssistant: "",
  evidence: [{ id: "e1", kind: "user", text: evidenceText, locator: "turn:t1",
    sha256: createHash("sha256").update(evidenceText).digest("hex"), observedAt: "2026-09-14T12:00:00Z" }],
  allowedPageIds: ["concepts/prior"] } as FlowJob;
const claims: FlowClaim[] = Array.from({ length: 5 }, (_, index) => ({ text: `项目决策 ${index}`,
  evidenceId: "e1", quote: evidenceText, title: `决策 ${index}`, topic: "项目", slug: `decision-${index}`,
  targetPageId: null, kind: "decision", status: "decided", useWhen: "后续实施", rationale: "改变执行", replacementIntent: false }));

function providerReturning(value: unknown) {
  const toolCall = vi.fn<LLMProvider["toolCall"]>().mockResolvedValue(JSON.stringify(value));
  return { provider: { toolCall } as unknown as LLMProvider, toolCall };
}

describe("knowledge flow five-claim limit", () => {
  it("requests and normalizes five evidence-bound claims", async () => {
    const { provider, toolCall } = providerReturning({ claims });
    expect(await extractClaims(provider, job, new Map(), 5)).toHaveLength(5);
    const call = toolCall.mock.calls[0];
    expect(call[2][0].input_schema).toMatchObject({ properties: { claims: { maxItems: 5 } } });
    expect(call[1][0].content).toContain("Maximum claims: 5");
  });

  it("rejects a sixth model claim even if the caller asks for more", async () => {
    const { provider } = providerReturning({ claims: [...claims, { ...claims[0], text: "第六条" }] });
    await expect(extractClaims(provider, job, new Map(), 10)).rejects.toThrow("exceeded proposal limit");
  });

  it("still honors a lower configured extraction limit", async () => {
    const { provider } = providerReturning({ claims });
    await expect(extractClaims(provider, job, new Map(), 2)).rejects.toThrow("exceeded proposal limit");
  });

  it("reviews index four and holds its reported conflict", async () => {
    const decisions = claims.map((_, index) => ({ index, decision: "accept", reason: "checked",
      conflictingPageIds: index === 4 ? ["concepts/prior"] : [] }));
    const { provider, toolCall } = providerReturning({ decisions });
    const result = await reviewClaims(provider, job, claims, new Map([["concepts/prior", "Old rule"]]));
    expect(result).toHaveLength(5);
    expect(result[4]).toMatchObject({ index: 4, decision: "needs_review" });
    expect(toolCall.mock.calls[0][2][0].input_schema).toMatchObject({ properties: { decisions: {
      maxItems: 5, items: { properties: { index: { maximum: 4 } } } } } });
  });
});
