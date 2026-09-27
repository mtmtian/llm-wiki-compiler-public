/** Topic generation owns selected content only and always rebuilds changed inputs from baseline. */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { topicRecord, topicFixture, topicFiles } from "./knowledge-flow-topic-fixtures.js";

describe("topic rebuild ownership", () => {
  it("Given cross-page title mentions, Then baseline prose outside the selected page remains byte-identical", async () => {
    const config = await topicFixture();
    const original = "---\ntitle: Other Concept\n---\n\nHuman text mentions Target Decision.\n";
    await writeFile(path.join(config.wikiRoot, "wiki/concepts/other.md"), original);
    const record = topicRecord("a", ["The Target Decision refers to Other Concept."], { title: "Target Decision" });
    const result = await materializeRecords(config, [record]);
    expect(result.conflicts).toEqual([]);
    expect((await topicFiles(config)).get("other.md")).toBe(original);
    const body = [...(await topicFiles(config)).values()].find(value => value.includes("projectId: companion"))!;
    expect(body).toContain(record.payload.claims[0].text);
  });

  it("Given a used generation root, When the record set changes, Then replay refuses it before claiming a successful new view", async () => {
    const config = await topicFixture(); const first = topicRecord("a", ["Requires approval"]);
    await materializeRecords(config, [first]);
    await expect(materializeRecords(config, [first, topicRecord("b", ["No approval required"])]))
      .rejects.toThrow(/fresh baseline/i);
  });

  it("Given a later conflicting packet, When a new stage starts from baseline, Then the earlier claim, source and mapping are absent", async () => {
    const prior = await topicFixture(); const fresh = await topicFixture();
    const first = topicRecord("a", ["Requires approval"]); const other = topicRecord("b", ["No approval required"]);
    await materializeRecords(prior, [first]);
    expect((await topicFiles(prior)).size).toBe(1);
    const result = await materializeRecords(fresh, [first, other]);
    expect(result.conflicts).toHaveLength(1); expect(result.pages).toBe(0);
    expect((await topicFiles(fresh, "sources")).size).toBe(0);
    const state = JSON.parse(await readFile(path.join(fresh.wikiRoot, ".llmwiki/state.json"), "utf8").catch(() => '{"sources":{}}'));
    expect(Object.keys(state.sources)).toEqual([]);
  });
});
