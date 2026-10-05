/**
 * Exercise empty review scopes at the real Codex process boundary. Empty arrays
 * must remain valid Structured Outputs schemas while rejecting invented coverage.
 */
import Ajv from "ajv";
import { expect, it } from "vitest";
import { createTopicReviewTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { codexToolCall } from "./fixtures/codex-tool-call.js";

it.each([
  { claims: 1, pages: ["concepts/topic"], retired: [] },
  { claims: 0, pages: ["concepts/topic"], retired: ["^[old.md:1]"] },
  { claims: 0, pages: [], retired: [] },
])("Given an empty review scope $claims/$pages/$retired, When sent to Codex, Then wire schema supports emptiness without allowing invented coverage", async ({ claims, pages, retired }) => {
  const review = { decision: "accept", reason: "checked", checkedClaimIndexes: claims ? [0] : [],
    checkedPageIds: pages, checkedRetiredCitations: retired,
    claimDecisions: claims ? [{ claimIndex: 0, decision: "accept", reason: "supported" }] : [],
    quoteRepairs: [], replaceEvidenceForClaims: [] };
  const tool = createTopicReviewTool(claims, pages, retired);
  const call = await codexToolCall(tool, review, "Review supplied edits", "Review");
  expect(call.output).toEqual(review);
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
    expect(accepts({ ...review, replaceEvidenceForClaims: [claims] })).toBe(false);
  }
  const { claimDecisions: _omitted, ...withoutConclusions } = review;
  expect(validate(withoutConclusions)).toBe(true);
  expect(wireValidate(withoutConclusions)).toBe(false);
});
