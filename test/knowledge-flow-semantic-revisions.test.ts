/** Given explicit semantic scope, cross-project page updates preserve provenance and safeguards. */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

const PAGE = "concepts/release-topic-abc123";
const OLD_TOPIC_ID = "e".repeat(64);
const OLD_PAGE = `---\ntitle: 发布流程\nprojectId: project-a\nprojectLabel: Project A\nsourceProjectIds:\n  - project-a\n  - project-z\nknowledgeTopicId: ${OLD_TOPIC_ID}\nknowledgePublicationRefs:\n  - prior-publication:0\n---\n\n旧结论 ^[prior-evidence.md:1]\n`;

function revisionRecord(id: string, projectId: string, basisHash: string | null, scope?: "semantic",
  basisRecordIds: string[] = []): PublicationRecord {
  const text = `${projectId} 的新证据`;
  const revision = { pageId: PAGE, topicId: stableTopicId(projectId, "发布流程", "小批量发布"),
    title: "发布流程", topic: "发布流程", decisionObject: "小批量发布", basisHash,
    body: `## 发布流程\n\n${text} {{claim:0}}\n\n旧结论 ^[prior-evidence.md:1]`, claimIndexes: [0],
    ...(scope ? { topicScope: scope } : {}) };
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a",
    projectId, projectLabel: projectId === "project-b" ? "Project B" : "Project A",
    createdAt: `2026-09-2${id === "a" ? "5" : "6"}T00:00:00Z`, originJobHash: id.repeat(64),
    repoIdentity: null, basisRecordIds, review: { status: "accepted", model: "test" },
    claims: [{ text, quote: text, evidenceId: "e1", title: "发布策略", topic: "发布流程",
      decisionObject: "小批量发布", slug: "release", targetPageId: PAGE, kind: "decision", status: "decided",
      useWhen: "发布新版本时", rationale: "验证发布效果" }],
    evidence: [{ id: "e1", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text),
      locator: `knowledge-evidence://${projectId}/release`, observedAt: "2026-09-26T00:00:00Z" }],
    topicRevisions: [revision] } };
}

async function fixture(): Promise<FlowConfig> {
  return makeKnowledgeFlowConfig("wiki-semantic-revision-");
}

async function writeLegacyPage(config: FlowConfig): Promise<void> {
  await writeFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), OLD_PAGE);
}

async function expectHeld(config: FlowConfig, records: PublicationRecord[], reason: string): Promise<void> {
  const result = await materializeRecords(config, records);
  expect(result.conflicts.some(conflict => conflict.reason.includes(reason))).toBe(true);
  expect(await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8")).toBe(OLD_PAGE);
}

async function renderedSemanticPage(): Promise<{ record: PublicationRecord; body: string }> {
  const seed = await fixture(); await writeLegacyPage(seed);
  const record = revisionRecord("b", "project-b", sha256Text(OLD_PAGE), "semantic");
  await materializeRecords(seed, [record]);
  const body = await readFile(path.join(seed.wikiRoot, "wiki", `${PAGE}.md`), "utf8");
  return { record, body };
}

describe("semantic topic revisions", () => {
  it("Given a Project A page, When Project B applies an explicit semantic revision, Then identity and both evidence sources survive", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    config.projects = { "project-a": { label: "Project A", pages: [PAGE] } };
    const record = revisionRecord("b", "project-b", sha256Text(OLD_PAGE), "semantic");
    const result = await materializeRecords(config, [record]);
    const page = await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8");
    const meta = parseFrontmatter(page).meta;
    expect(result.conflicts).toEqual([]);
    expect(meta.topicScope).toBe("semantic");
    expect(meta.sourceProjectIds).toEqual(["project-a", "project-b", "project-z"]);
    expect(meta).not.toHaveProperty("projectId"); expect(meta).not.toHaveProperty("projectLabel");
    expect(meta.knowledgeTopicId).toBe(OLD_TOPIC_ID);
    expect(page).toContain("prior-evidence.md:1"); expect(page).toContain("project-b 的新证据");
    expect(page).toContain("knowledgePublicationRefs:");
  });

  it("Given a Project A page, When Project B uses a legacy revision, Then the cross-project write is rejected", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    const record = revisionRecord("b", "project-b", sha256Text(OLD_PAGE));
    await expect(materializeRecords(config, [record])).rejects.toThrow(/project boundary|ownership/i);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8")).toBe(OLD_PAGE);
  });

  it("Given a semantic page already containing a revision, When that record is replayed, Then the page is unchanged without a conflict", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    const record = revisionRecord("b", "project-b", sha256Text(OLD_PAGE), "semantic");
    await materializeRecords(config, [record]);
    const before = await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8");
    const replay = await materializeRecords(config, [record]);
    expect(replay.conflicts).toEqual([]);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8")).toBe(before);
  });

  it("Given a semantic page, When an unmarked revision targets it, Then the page cannot be downgraded", async () => {
    const { record: semantic, body: before } = await renderedSemanticPage();
    const config = await fixture();
    await writeFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), before);
    const legacy = revisionRecord("c", "project-b", sha256Text(before), undefined, [semantic.id]);
    const result = await materializeRecords(config, [semantic, legacy]);
    expect(result.conflicts.some(conflict => conflict.reason.includes("legacy") || conflict.reason.includes("basis"))).toBe(true);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8")).toBe(before);
  });

  it("Given concurrent semantic revisions, When neither observes the other, Then both are held", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    const first = revisionRecord("b", "project-b", sha256Text(OLD_PAGE), "semantic");
    const second = revisionRecord("c", "project-c", sha256Text(OLD_PAGE), "semantic");
    await expectHeld(config, [first, second], "concurrent");
  });

  it("Given a semantic revision with a stale basis hash, When materialized, Then it is held", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    const record = revisionRecord("b", "project-b", "f".repeat(64), "semantic");
    await expectHeld(config, [record], "basis hash");
  });

  it("Given semantic then legacy revisions, When replayed from the old baseline, Then only the legacy successor is held", async () => {
    const { record: semantic, body: expected } = await renderedSemanticPage();
    const legacy = revisionRecord("c", "project-a", sha256Text(expected), undefined, [semantic.id]);
    const config = await fixture(); await writeLegacyPage(config);
    const result = await materializeRecords(config, [semantic, legacy]);
    expect(result.conflicts).toEqual([expect.objectContaining({ recordIds: [legacy.id], reason: expect.stringContaining("legacy") })]);
    expect(await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8")).toBe(expected);
  });

  it("Given semantic scope with no new publications, When materialized, Then topic navigation is rebuilt", async () => {
    const config = await fixture(); await writeLegacyPage(config);
    config.topicScope = "semantic";
    config.projects = { "project-a": { label: "Project A", pages: [PAGE] } };
    await materializeRecords(config, []);
    const navigation = await readFile(path.join(config.wikiRoot, "wiki", "MOC.md"), "utf8");
    expect(navigation).toContain("# 主题知识导航");
    expect(navigation).toContain("来源项目：Project A、project-z");
  });
});
