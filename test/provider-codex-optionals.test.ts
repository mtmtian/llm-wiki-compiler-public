/**
 * Codex process-boundary regression tests for optional Structured Outputs fields.
 * A strict transport must represent omission without forcing invented metadata,
 * while original required fields, nullable values and branch contracts survive.
 */
import Ajv from "ajv";
import { expect, it } from "vitest";
import { CONCEPT_EXTRACTION_TOOL } from "../src/compiler/prompts.js";
import { createTopicReviewTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import type { LLMTool } from "../src/utils/provider.js";
import { codexToolCall } from "./fixtures/codex-tool-call.js";

/** Exercise a supplied schema through the real provider and a fake Codex process. */
function call(schema: Record<string, unknown>, output: unknown) {
  const tool: LLMTool = { name: "optional_result", description: "Return a result", input_schema: schema };
  return codexToolCall(tool, output, "Return the requested structured result.");
}

const SCHEMA = {
  type: "object", additionalProperties: false, required: ["id"],
  properties: {
    id: { type: "string" },
    label: { type: "string", enum: ["low", "high"] },
    retainedNull: { anyOf: [{ type: "string" }, { type: "null" }] },
    entries: { type: "array", items: { type: "object", required: ["name"],
      properties: { name: { type: "string" }, note: { type: "string" } }, additionalProperties: false } },
  },
};

it("restores optional omissions at every depth while retaining an originally valid null", async () => {
  const original = structuredClone(SCHEMA);
  const response = { id: "x", label: null, retainedNull: null, entries: [{ name: "item", note: null }] };
  const result = await call(SCHEMA, response);
  expect(result.output).toEqual({ id: "x", retainedNull: null, entries: [{ name: "item" }] });
  expect(SCHEMA).toEqual(original);
  const wire = new Ajv({ strict: false }).compile(result.schema as object);
  expect(wire(response)).toBe(true);
  expect(wire({ id: "x" })).toBe(false);
});

it("still rejects a null required value and invalid optional values", async () => {
  for (const response of [{ id: null }, { id: "x", label: 42 }]) {
    await expect(call(SCHEMA, response)).rejects.toThrow(/schema validation/);
  }
});

it("accepts omitted metadata from the actual concept extraction schema", async () => {
  const concept = { concept: "Lantern", summary: "A team", is_new: true, tags: null,
    confidence: null, provenance_state: null, contradicted_by: [{ slug: "other", reason: null }] };
  const result = await call(CONCEPT_EXTRACTION_TOOL.input_schema,
    { disposition: null, reason: null, concepts: [concept] });
  expect(result.output).toEqual({ concepts: [{ concept: "Lantern", summary: "A team", is_new: true,
    contradicted_by: [{ slug: "other" }] }] });
});

it("restores nullable review metadata without dropping required coverage", async () => {
  const tool = createTopicReviewTool(0, [], []);
  const response = { decision: "accept", reason: "No edits", checkedClaimIndexes: [], checkedPageIds: [],
    checkedRetiredCitations: null, claimDecisions: null, replaceEvidenceForClaims: null, quoteRepairs: null };
  const result = await call(tool.input_schema, response);
  expect(result.output).toEqual({ decision: "accept", reason: "No edits", checkedClaimIndexes: [], checkedPageIds: [] });
  await expect(call(tool.input_schema, { ...response, checkedPageIds: null })).rejects.toThrow(/schema validation/);
});

it("restores optional fields inside a referenced union without accepting a wrong discriminator", async () => {
  const schema = { type: "object", required: ["entry"], properties: { entry: { $ref: "#/$defs/entry" } },
    $defs: { entry: { anyOf: [
      { type: "object", properties: { kind: { const: "text" }, note: { type: "string" } }, required: ["kind"] },
      { type: "object", properties: { kind: { const: "count" }, count: { type: "number" } }, required: ["kind", "count"] },
    ] } } };
  const result = await call(schema, { entry: { kind: "text", note: null } });
  expect(result.output).toEqual({ entry: { kind: "text" } });
  await expect(call(schema, { entry: { kind: "count", count: null } })).rejects.toThrow(/schema validation/);
});


it("keeps enum/default payloads literal and allows omission of an optional const", async () => {
  const literal = { properties: { nested: { type: "string" } }, required: ["unchanged"] };
  const schema = { type: "object", properties: {
    payload: { enum: [literal], default: literal },
    fixed: { type: "string", const: "fixed" }, nothing: { type: "null" },
  }, required: ["payload"] };
  const response = { payload: literal, fixed: null, nothing: null };
  const result = await call(schema, response);
  expect(result.output).toEqual({ payload: literal, nothing: null });
  expect(new Ajv({ strict: false }).validate(result.schema as object, response)).toBe(true);
  expect(JSON.stringify(result.schema)).toContain(JSON.stringify(literal));
  expect(schema.properties.payload.default).toEqual(literal);
});
