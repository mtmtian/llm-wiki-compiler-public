/** Given/When/Then contracts for role-scoped correction tool schemas. */
import Ajv from "ajv";
import { describe, expect, it } from "vitest";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { createQuoteBoundEditTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { createCorrectionPatchTool } from "../extensions/knowledge-flow/claim-patch.js";
import type { StableClaimEntry } from "../extensions/knowledge-flow/consolidation-draft.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { codexToolCall } from "./fixtures/codex-tool-call.js";

const PAGE = "concepts/authority";

function evidence(id: string, kind: FlowEvidence["kind"]): FlowEvidence {
  const text = `${kind} evidence ${id}`;
  return { id, kind, text, locator: `turn:${id}`, observedAt: "2026-09-21T00:00:00Z", sha256: sha256Text(text) };
}

function catalog(...items: FlowEvidence[]) { return buildCorrectionEvidence(items); }

function claim(catalogue: ReturnType<typeof catalog>, primary: string, kind: string, status: string, support?: string) {
  const primaryItem = catalogue.find(item => item.id === primary)!;
  const supportItem = support ? catalogue.find(item => item.id === support)! : undefined;
  return { text: "claim", quoteId: primaryItem.quoteOptions[0].quoteId,
    title: "Topic", slug: "topic", targetPageId: PAGE,
    kind, status, useWhen: "when needed", rationale: "because", replacementIntent: false,
    supportingQuotes: supportItem ? [{ quoteId: supportItem.quoteOptions[0].quoteId }] : [] };
}

function draft(claims: unknown[]) {
  return { claims, pages: [{ pageId: PAGE, body: "{{claim:0}}", claimIndexes: [0] }], summary: "summary" };
}

function validator(items: FlowEvidence[]) {
  return new Ajv({ allErrors: true, strict: false }).compile(createQuoteBoundEditTool([PAGE], catalog(...items)).input_schema);
}

function stableEntry(source: FlowEvidence): StableClaimEntry {
  const sourceCatalog = catalog(source);
  return { claimId: "c0", quoteId: sourceCatalog[0].quoteOptions[0].quoteId, supportingQuoteIds: [],
    claim: { text: source.text, evidenceId: source.id, quote: source.text, title: "Topic", topic: "Topic",
      decisionObject: "Object", slug: "topic", targetPageId: PAGE, kind: "decision", status: "decided",
      useWhen: "when needed", rationale: "because", replacementIntent: false } };
}

function expectNoMutableClaims(schema: unknown): void {
  const properties = (schema as any).properties;
  expect(properties.claimUpdates.maxItems).toBe(0);
  expect(properties.droppedClaimIds.maxItems).toBe(0);
  expect(properties.droppedClaimIds.items).toMatchObject({ type: "string", minLength: 1 });
  expect(JSON.stringify(schema)).not.toContain('"not"');
}

describe("role-scoped quote-bound edit schema", () => {
  it("keeps each patch field edit independently required through the Codex strict-schema adapter", async () => {
    const source = evidence("user", "user");
    const sourceCatalog = catalog(source);
    const entry = stableEntry(source);
    const tool = createCorrectionPatchTool([PAGE], sourceCatalog, [entry], { lockedClaimIds: [], replaceEvidenceForClaimIds: [] });
    const output = { claimUpdates: [{ claimId: "c0", changes: [{ field: "text", value: "corrected claim" }] }],
      droppedClaimIds: [], pages: [{ pageId: PAGE, body: "{{claim:c0}}", claimIds: ["c0"] }], summary: "correction" };
    const call = await codexToolCall(tool, output, "Patch a claim", "Correct its wording.");
    expect(call.output).toEqual(output);
    const update = (call.schema as any).properties.claimUpdates.items.anyOf[0];
    expect(update.required).toEqual(["claimId", "changes"]);
    for (const fieldChange of update.properties.changes.items.anyOf) {
      expect(fieldChange.required).toEqual(["field", "value"]);
    }
  });

  it("keeps empty correction scopes valid for Codex when there are no claims to edit", async () => {
    const output = { claimUpdates: [], droppedClaimIds: [],
      pages: [{ pageId: PAGE, body: "本页没有新的主张。", claimIds: [] }], summary: "无需新增主张。" };
    const tool = createCorrectionPatchTool([PAGE], [], [], { lockedClaimIds: [], replaceEvidenceForClaimIds: [] });
    const call = await codexToolCall(tool, output, "Return an empty claim patch.", "No supported claims remain.");
    expect(call.output).toEqual(output);
    expectNoMutableClaims(call.schema);
    const schema = call.schema as any;
    expect(schema.properties.pages.items.properties.claimIds.maxItems).toBe(0);
    expect(schema.properties.pages.items.properties.claimIds.items).toMatchObject({ type: "string", minLength: 1 });
    expect(schema.properties.pages.items.properties.citationRetirements.items.properties.replacement.anyOf)
      .toHaveLength(2);
  });

  it("keeps dropped-claim selectors valid when every reviewed claim is locked", async () => {
    const source = evidence("locked-user", "user");
    const sourceCatalog = catalog(source);
    const entry = stableEntry(source);
    const output = { claimUpdates: [], droppedClaimIds: [],
      pages: [{ pageId: PAGE, body: "{{claim:c0}}", claimIds: ["c0"] }], summary: "保留已锁定主张。" };
    const tool = createCorrectionPatchTool([PAGE], sourceCatalog, [entry],
      { lockedClaimIds: ["c0"], replaceEvidenceForClaimIds: [] });
    const call = await codexToolCall(tool, output, "Keep locked claim unchanged.", "The claim was accepted.");
    expect(call.output).toEqual(output);
    expectNoMutableClaims(call.schema);
    const schema = call.schema as any;
    expect(schema.properties.pages.items.properties.claimIds.items.enum).toEqual(["c0"]);
  });

  it("rejects assistant primary plus assistant support and assistant facts", () => {
    const items = [evidence("a1", "assistant"), evidence("a2", "assistant")]; const validate = validator(items);
    expect(validate(draft([claim(catalog(...items), "a1", "lesson", "historical", "a2")]))).toBe(false);
    expect(validate(draft([claim(catalog(...items), "a1", "fact", "historical")]))).toBe(false);
    expect(validate(draft([claim(catalog(...items), "a1", "lesson", "decided")]))).toBe(false);
  });

  it("accepts user approval with assistant support, artifact history, and assistant lessons", () => {
    const mixed = [evidence("u", "user"), evidence("a", "assistant"), evidence("f", "artifact")];
    const validate = validator(mixed); const mixedCatalog = catalog(...mixed);
    expect(validate(draft([claim(mixedCatalog, "u", "decision", "decided", "a")]))).toBe(true);
    expect(validate(draft([claim(mixedCatalog, "f", "fact", "historical")]))).toBe(true);
    expect(validate(draft([claim(mixedCatalog, "a", "lesson", "historical")]))).toBe(true);
    expect(validate(draft([claim(mixedCatalog, "f", "decision", "historical")]))).toBe(false);
    expect(validate(draft([claim(mixedCatalog, "u", "decision", "uncertain")]))).toBe(true);
  });

  it("rejects assistant support IDs for non-user primaries and permits user support IDs", () => {
    const items = [evidence("a", "assistant"), evidence("u", "user"), evidence("f", "artifact")];
    const validate = validator(items); const itemsCatalog = catalog(...items);
    expect(validate(draft([claim(itemsCatalog, "a", "lesson", "historical", "a")]))).toBe(false);
    expect(validate(draft([claim(itemsCatalog, "a", "lesson", "historical", "u")]))).toBe(true);
    expect(validate(draft([claim(itemsCatalog, "f", "fact", "historical", "a")]))).toBe(false);
  });

  it("keeps non-user-only catalogs valid and lets the empty catalog emit no claims", () => {
    const items = [evidence("a", "assistant"), evidence("f", "artifact")]; const validate = validator(items);
    const branches = (createQuoteBoundEditTool([PAGE], catalog(...items)).input_schema as any).properties.claims.items.anyOf;
    expect(branches).toHaveLength(2);
    expect(validate(draft([claim(catalog(...items), "a", "lesson", "historical")]))).toBe(true);
    expect(validate(draft([claim(catalog(...items), "f", "constraint", "historical")]))).toBe(true);
    const empty = new Ajv({ allErrors: true, strict: false }).compile(createQuoteBoundEditTool([PAGE], []).input_schema);
    expect(empty(draft([]))).toBe(true);
    expect(JSON.stringify(createQuoteBoundEditTool([PAGE], []).input_schema)).not.toContain('"not"');
  });

  it("uses maxItems rather than unsupported schemas when non-user primaries have no support", () => {
    const schema = createQuoteBoundEditTool([PAGE], catalog(evidence("a", "assistant"))).input_schema as any;
    const branch = schema.properties.claims.items.anyOf[0];
    expect(branch.properties.supportingQuotes.maxItems).toBe(0);
    expect(JSON.stringify(schema)).not.toContain('"not"');
  });

  it("rejects a second source selector that could disagree with the chosen quote", () => {
    const items = [evidence("a1", "assistant"), evidence("a2", "assistant"), evidence("u", "user")];
    const source = catalog(...items); const validate = validator(items);
    const item = (createQuoteBoundEditTool([PAGE], source).input_schema as any).properties.claims.items.anyOf[0];
    expect(item.properties).not.toHaveProperty("topic"); expect(item.properties).not.toHaveProperty("decisionObject");
    expect(item.required).toContain("targetPageId");
    const valid = claim(source, "u", "decision", "decided", "a1");
    expect(validate(draft([valid]))).toBe(true);
    expect(validate(draft([{ ...valid, topic: "model topic" }]))).toBe(false);
    expect(validate(draft([{ ...valid, evidenceId: "a2" }]))).toBe(false);
    expect(validate(draft([{ ...valid, supportingQuotes: [{ ...valid.supportingQuotes[0], evidenceId: "a2" }] }]))).toBe(false);
  });

  it("returns quote selectors and no model-owned source text or page metadata", () => {
    const item = evidence("a", "assistant");
    const branch = (createQuoteBoundEditTool([PAGE], catalog(item)).input_schema as any).properties.claims.items.anyOf[0];
    expect(branch.properties.quoteId.enum).toEqual(catalog(item)[0].quoteOptions.map(option => option.quoteId));
    for (const field of ["quote", "evidenceId", "topic", "decisionObject"]) {
      expect(branch.properties).not.toHaveProperty(field);
    }
  });
});
