/** Given/When/Then contracts for role-scoped correction tool schemas. */
import Ajv from "ajv";
import { describe, expect, it } from "vitest";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { createCorrectionEditTool, editTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";

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
  return new Ajv({ allErrors: true, strict: false }).compile(createCorrectionEditTool([PAGE], catalog(...items)).input_schema);
}

describe("role-scoped correction schema", () => {
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
    expect(validate(draft([claim(mixedCatalog, "u", "decision", "uncertain")]))).toBe(false);
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
    const branches = (createCorrectionEditTool([PAGE], catalog(...items)).input_schema as any).properties.claims.items.anyOf;
    expect(branches).toHaveLength(2);
    expect(validate(draft([claim(catalog(...items), "a", "lesson", "historical")]))).toBe(true);
    expect(validate(draft([claim(catalog(...items), "f", "constraint", "historical")]))).toBe(true);
    const empty = new Ajv({ allErrors: true, strict: false }).compile(createCorrectionEditTool([PAGE], []).input_schema);
    expect(empty(draft([]))).toBe(true);
    expect(JSON.stringify(createCorrectionEditTool([PAGE], []).input_schema)).not.toContain('"not"');
  });

  it("uses maxItems rather than unsupported schemas when non-user primaries have no support", () => {
    const schema = createCorrectionEditTool([PAGE], catalog(evidence("a", "assistant"))).input_schema as any;
    const branch = schema.properties.claims.items.anyOf[0];
    expect(branch.properties.supportingQuotes.maxItems).toBe(0);
    expect(JSON.stringify(schema)).not.toContain('"not"');
  });

  it("rejects a second source selector that could disagree with the chosen quote", () => {
    const items = [evidence("a1", "assistant"), evidence("a2", "assistant"), evidence("u", "user")];
    const source = catalog(...items); const validate = validator(items);
    const item = (createCorrectionEditTool([PAGE], source).input_schema as any).properties.claims.items.anyOf[0];
    expect(item.properties).not.toHaveProperty("topic"); expect(item.properties).not.toHaveProperty("decisionObject");
    expect(item.required).toContain("targetPageId");
    const valid = claim(source, "u", "decision", "decided", "a1");
    expect(validate(draft([valid]))).toBe(true);
    expect(validate(draft([{ ...valid, topic: "model topic" }]))).toBe(false);
    expect(validate(draft([{ ...valid, evidenceId: "a2" }]))).toBe(false);
    expect(validate(draft([{ ...valid, supportingQuotes: [{ ...valid.supportingQuotes[0], evidenceId: "a2" }] }]))).toBe(false);
  });

  it("shows the old unconstrained schema accepted an authority-invalid shape", () => {
    const item = evidence("a", "assistant");
    const bad = { ...claim(catalog(item), "a", "fact", "decided"), topic: "Topic", decisionObject: "Object",
      evidenceId: item.id, quote: item.text };
    delete (bad as Record<string, unknown>).quoteId;
    expect(new Ajv({ allErrors: true, strict: false }).compile(editTool.input_schema)(draft([bad]))).toBe(true);
  });
});
