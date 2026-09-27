/** Given immutable peer records, local replay must preserve evidence and isolate concurrent variants. */
import { readdir, readFile, rm } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords, publicationConflicts } from "../extensions/knowledge-flow/materialize.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

function record(id: string, text: string, machineId = "a", basis: string[] = [], topic = "release"): PublicationRecord {
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId,
    projectId: "example", projectLabel: "Example", createdAt: "2026-09-15T00:00:00Z", originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds: basis, review: { status: "accepted", model: "test" },
    claims: [{ text, quote: text, evidenceId: "e1", title: "Release decision", topic, slug: "release",
      targetPageId: null, kind: "decision", status: "decided", useWhen: "Deploying the service", rationale: "Preserve the decision" }],
    evidence: [{ id: "e1", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text),
      locator: `knowledge-evidence://${machineId}/abc`, observedAt: "2026-09-15T00:00:00Z" }] } };
}

const fixture = (): Promise<FlowConfig> => makeKnowledgeFlowConfig("wiki-local-view-");

/** Invalid packets must fail before writing any topic page. */
async function expectRejectedPublication(invalid: PublicationRecord): Promise<void> {
  const config = await fixture();
  await expect(materializeRecords(config, [invalid])).rejects.toThrow(/evidence|claims/i);
  expect(await readdir(path.join(config.wikiRoot, "wiki/concepts"))).toEqual([]);
}

describe("immutable publication local views", () => {
  it("Given a user approval with an assistant proposal, Then legacy replay retains both original quotes", async () => {
    const accepted = record("a", "Adopt the proposed rollback plan");
    const proposal = "Keep the previous release available for rollback";
    accepted.payload.claims[0].supportingQuotes = [{ evidenceId: "e2", quote: proposal }];
    accepted.payload.evidence.push({ ...accepted.payload.evidence[0], id: "e2", kind: "assistant", text: proposal,
      sha256: sha256Text(proposal), originalSha256: sha256Text(proposal) });
    const config = await fixture();
    expect((await materializeRecords(config, [accepted])).pages).toBe(1);
    const [source] = await readdir(path.join(config.wikiRoot, "sources"));
    expect(await readFile(path.join(config.wikiRoot, "sources", source), "utf8")).toContain(proposal);
  });

  it("Given offline opposite decisions, When replay order changes, Then both stay outside the accepted view", async () => {
    const a = record("a", "Deploy only after approval"); const b = record("b", "Deploy without approval", "b");
    expect(publicationConflicts([a, b])).toEqual(publicationConflicts([b, a]));
    expect(publicationConflicts([a, b])[0].recordIds).toEqual([a.id, b.id]);
    const config = await fixture();
    const result = await materializeRecords(config, [a, b]);
    expect(result.conflicts).toHaveLength(1);
    expect(await readdir(path.join(config.wikiRoot, "wiki/concepts"))).toEqual([]);
    expect(await readFile(path.join(config.wikiRoot, "wiki/index.md"), "utf8")).not.toContain("Deploy without approval");
  });

  it("Given independent topics, When two machines replay the same records, Then page and source contents agree", async () => {
    const records = [record("a", "Deploy after approval"), record("b", "Keep stable project IDs", "b", [], "identity")];
    const left = await fixture(); const right = await fixture();
    expect((await materializeRecords(left, records)).pages).toBe(2);
    expect((await materializeRecords(right, [...records].reverse())).pages).toBe(2);
    for (const name of ["wiki/index.md", "wiki/MOC.md"]) {
      expect(await readFile(path.join(left.wikiRoot, name), "utf8"))
        .toBe(await readFile(path.join(right.wikiRoot, name), "utf8"));
    }
    for (const folder of ["wiki/concepts", "sources"]) {
      const files = (await readdir(path.join(left.wikiRoot, folder))).sort();
      expect((await readdir(path.join(right.wikiRoot, folder))).sort()).toEqual(files);
      for (const file of files) expect(await readFile(path.join(left.wikiRoot, folder, file), "utf8"))
        .toBe(await readFile(path.join(right.wikiRoot, folder, file), "utf8"));
    }
  });

  it("Given the same statement with different source evidence, Then both sources survive and replay is idempotent", async () => {
    const a = record("a", "Keep stable IDs"); const b = record("b", "Keep stable IDs", "b");
    b.payload.evidence[0].originalSha256 = "d".repeat(64);
    const config = await fixture();
    await materializeRecords(config, [a, b]); await materializeRecords(config, [a, b]);
    expect(await readdir(path.join(config.wikiRoot, "sources"))).toHaveLength(2);
    const bodies = await Promise.all((await readdir(path.join(config.wikiRoot, "sources")))
      .map(name => readFile(path.join(config.wikiRoot, "sources", name), "utf8")));
    expect(bodies.join("\n")).toContain("d".repeat(64));
  });

  it("Given changed applicability, Then it is retained as a variant rather than silently deduplicated", () => {
    const a = record("a", "Keep stable IDs"); const b = record("b", "Keep stable IDs", "b");
    b.payload.claims[0].useWhen = "Only in staging";
    expect(publicationConflicts([a, b])).toHaveLength(1);
    b.payload.basisRecordIds = [a.id];
    expect(publicationConflicts([a, b])).toEqual([]);
  });

  it("Given assistant decisions or unsupported quote records, Then no page can be materialized", async () => {
    const invalid = record("a", "Must use stable IDs"); invalid.payload.evidence[0].kind = "assistant";
    await expectRejectedPublication(invalid);
  });

  it("Given an artifact primary with assistant support, Then replay rejects the packet before rendering", async () => {
    const invalid = record("a", "Artifact report"); const support = "Assistant context";
    invalid.payload.evidence[0].kind = "artifact"; invalid.payload.claims[0].kind = "fact";
    invalid.payload.claims[0].status = "historical";
    invalid.payload.claims[0].supportingQuotes = [{ evidenceId: "e2", quote: support }];
    invalid.payload.evidence.push({ ...invalid.payload.evidence[0], id: "e2", kind: "assistant", text: support,
      sha256: sha256Text(support), originalSha256: sha256Text(support) });
    await expectRejectedPublication(invalid);
  });

  it("Given a dated assistant lesson, Then the local view keeps its exact source and historical status", async () => {
    const analysis = record("a", "Separate acquisition cost from retention");
    analysis.payload.evidence[0].kind = "assistant";
    Object.assign(analysis.payload.claims[0], { kind: "lesson", status: "historical" });
    const config = await fixture();
    expect((await materializeRecords(config, [analysis])).pages).toBe(1);
    const [page] = await readdir(path.join(config.wikiRoot, "wiki/concepts"));
    expect(await readFile(path.join(config.wikiRoot, "wiki/concepts", page), "utf8")).toContain("historical");
    analysis.payload.claims[0].status = "decided";
    expect(() => publicationConflicts([analysis])).toThrow(/evidence|claims/i);
  });

  it("Given an empty baseline, Then the first replica can initialize without any publications", async () => {
    const config = await fixture();
    await rm(path.join(config.wikiRoot, "wiki"), { recursive: true });
    expect(await materializeRecords(config, [])).toEqual({ pages: 0, conflicts: [] });
    expect(await readdir(path.join(config.wikiRoot, "wiki/concepts"))).toEqual([]);
  });

});
