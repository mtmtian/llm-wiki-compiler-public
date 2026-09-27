/**
 * Regression coverage for the Codex Structured Outputs compatibility boundary.
 *
 * The fake `codex` process observes the wire schema while the provider still
 * validates the returned value against the caller's original JSON Schema.
 */

import { access } from "node:fs/promises";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { CodexAgentProvider } from "../src/providers/codex-agent.js";
import type { LLMTool } from "../src/utils/provider.js";
import { installFakeCodex, type FakeCodex } from "./fixtures/fake-codex.js";

const originalEnv = { ...process.env };
const fakes: FakeCodex[] = [];

const NESTED_TOOL: LLMTool = {
  name: "nested_result",
  description: "Return a nested result",
  input_schema: {
    type: "object",
    properties: {
      title: { type: "string" },
      metadata: {
        type: "object",
        properties: {
          source: { type: "string" },
          confidence: { type: "number" },
        },
        required: ["source"],
      },
      entries: {
        type: "array",
        items: {
          type: "object",
          properties: {
            slug: { type: "string" },
            note: { type: "string" },
          },
          required: ["slug"],
        },
      },
    },
    required: ["title"],
    additionalProperties: false,
  },
};

/** Install the fake at the literal PATH boundary used by the provider. */
async function useFake(toolOutput: unknown): Promise<FakeCodex> {
  const fake = await installFakeCodex({ toolOutput });
  fakes.push(fake);
  process.env.PATH = `${fake.binDir}${path.delimiter}${originalEnv.PATH ?? ""}`;
  return fake;
}

afterEach(async () => {
  process.env = { ...originalEnv };
  await Promise.all(fakes.splice(0).map((fake) => fake.cleanup()));
});

describe("CodexAgentProvider strict schema compatibility", () => {
  it("strictifies nested schemas without mutating the caller schema", async () => {
    const original = structuredClone(NESTED_TOOL.input_schema);
    const fake = await useFake({ title: "ok", metadata: { source: "source-a" } });
    const provider = new CodexAgentProvider(undefined, { timeoutMs: 2_000 });

    await expect(provider.toolCall("system", [{ role: "user", content: "x" }], [NESTED_TOOL], 99))
      .resolves.toBe('{"title":"ok","metadata":{"source":"source-a"}}');

    expect(NESTED_TOOL.input_schema).toEqual(original);
    const [call] = await fake.calls();
    expect(call.args).toContain("--output-schema");
    const wire = call.schema as {
      required: string[];
      additionalProperties: boolean;
      properties: {
        metadata: { required: string[]; additionalProperties: boolean };
        entries: { items: { required: string[]; additionalProperties: boolean } };
      };
    };
    expect(wire.required).toEqual(["title", "metadata", "entries"]);
    expect(wire.additionalProperties).toBe(false);
    expect(wire.properties.metadata.required).toEqual(["source", "confidence"]);
    expect(wire.properties.metadata.additionalProperties).toBe(false);
    expect(wire.properties.entries.items.required).toEqual(["slug", "note"]);
    expect(wire.properties.entries.items.additionalProperties).toBe(false);
    await expect(access(call.cwd)).rejects.toThrow();
  });

  it("keeps validating structured output against the original schema", async () => {
    const fake = await useFake({ title: "ok", metadata: { source: 42 } });
    const provider = new CodexAgentProvider(undefined, { timeoutMs: 2_000 });

    await expect(provider.toolCall("system", [{ role: "user", content: "x" }], [NESTED_TOOL], 99))
      .rejects.toThrow(/schema validation/i);
    const [call] = await fake.calls();
    await expect(access(call.cwd)).rejects.toThrow();
  });
});
