/**
 * Exercise empty review scopes at the real Codex process boundary. Empty arrays
 * must remain valid Structured Outputs schemas while rejecting invented coverage.
 */
import path from "node:path";
import Ajv from "ajv";
import { afterEach, expect, it, vi } from "vitest";
import { CodexAgentProvider } from "../src/providers/codex-agent.js";
import { createTopicReviewTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { installFakeCodex, type FakeCodex } from "./fixtures/fake-codex.js";

const fakes: FakeCodex[] = [];
afterEach(async () => {
  vi.unstubAllEnvs();
  await Promise.all(fakes.splice(0).map(fake => fake.cleanup()));
});

it.each([
  { claims: 1, pages: ["concepts/topic"], retired: [] },
  { claims: 0, pages: ["concepts/topic"], retired: ["^[old.md:1]"] },
  { claims: 0, pages: [], retired: [] },
])("Given an empty review scope $claims/$pages/$retired, When sent to Codex, Then wire schema supports emptiness without allowing invented coverage", async ({ claims, pages, retired }) => {
  const review = { decision: "accept", reason: "checked", checkedClaimIndexes: claims ? [0] : [],
    checkedPageIds: pages, checkedRetiredCitations: retired,
    claimDecisions: claims ? [{ claimIndex: 0, decision: "accept", reason: "supported" }] : [],
    quoteRepairs: [], retainEvidenceForClaims: [] };
  const tool = createTopicReviewTool(claims, pages, retired);
  const fake = await installFakeCodex({ toolOutput: review }); fakes.push(fake);
  vi.stubEnv("PATH", `${fake.binDir}${path.delimiter}${process.env.PATH ?? ""}`);
  const output = await new CodexAgentProvider().toolCall("Review supplied edits", [{ role: "user", content: "Review" }], [tool], 100);
  expect(JSON.parse(output)).toEqual(review);
  const [call] = await fake.calls();
  expect(JSON.stringify(call.schema)).not.toContain('"not":');
  expect(JSON.stringify(call.schema)).not.toContain('"uniqueItems":');
  const validate = new Ajv({ strict: false }).compile(tool.input_schema);
  const wireValidate = new Ajv({ strict: false }).compile(call.schema as object);
  for (const accepts of [validate, wireValidate]) {
    expect(accepts(review)).toBe(true);
    expect(accepts({ ...review, checkedClaimIndexes: [claims] })).toBe(false);
    expect(accepts({ ...review, checkedPageIds: ["concepts/invented"] })).toBe(false);
    expect(accepts({ ...review, checkedRetiredCitations: ["^[invented.md:1]"] })).toBe(false);
    expect(accepts({ ...review, claimDecisions: [{ claimIndex: claims, decision: "accept", reason: "invented" }] })).toBe(false);
    expect(accepts({ ...review, quoteRepairs: [{ claimIndex: 0, quoteId: "invented" }] })).toBe(false);
    expect(accepts({ ...review, retainEvidenceForClaims: [claims] })).toBe(false);
    expect(accepts({ ...review, retainEvidenceForClaims: [0, 0] })).toBe(false);
  }
  const { claimDecisions: _omitted, ...withoutConclusions } = review;
  expect(validate(withoutConclusions)).toBe(true);
  expect(wireValidate(withoutConclusions)).toBe(false);
});
