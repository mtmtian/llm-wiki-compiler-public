/**
 * Regression coverage for the knowledge-flow JSON process boundary.
 * Stream chunks intentionally split UTF-8 bytes to exercise the Node entry
 * path fixed in PR #6, while byte-cap tests use small injected limits.
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_EVENT_BYTE_LIMIT,
  MATERIALIZE_EVENT_BYTE_LIMIT,
  eventByteLimit,
  readBoundedJson,
} from "../extensions/knowledge-flow/stdin.js";

async function* oneByteChunks(bytes: Uint8Array): AsyncIterable<Uint8Array> {
  for (const byte of bytes) yield Uint8Array.of(byte);
}

describe("knowledge-flow JSON stdin", () => {
  it("decodes Chinese text when UTF-8 characters cross chunk boundaries", async () => {
    const expected = { prompt: "请保留这个决定：部署前必须人工复核 ✅" };
    const bytes = Buffer.from(JSON.stringify(expected), "utf8");
    await expect(readBoundedJson(oneByteChunks(bytes), bytes.byteLength)).resolves.toEqual(expected);
  });

  it("accepts an exact injected byte cap and rejects the next byte", async () => {
    const bytes = Buffer.from(JSON.stringify({ text: "边界" }), "utf8");
    await expect(readBoundedJson(oneByteChunks(bytes), bytes.byteLength)).resolves.toEqual({ text: "边界" });
    await expect(readBoundedJson(oneByteChunks(bytes), bytes.byteLength - 1)).rejects.toThrow("too large");
  });

  it("keeps the production caps explicit for ordinary and materialize operations", () => {
    expect(eventByteLimit("context")).toBe(DEFAULT_EVENT_BYTE_LIMIT);
    expect(eventByteLimit("materialize")).toBe(MATERIALIZE_EVENT_BYTE_LIMIT);
    expect(DEFAULT_EVENT_BYTE_LIMIT).toBe(600_000);
    expect(MATERIALIZE_EVENT_BYTE_LIMIT).toBe(32_000_000);
  });
});
