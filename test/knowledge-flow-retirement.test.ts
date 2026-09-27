/** Reviewed curation keeps durable evidence and cannot resurrect retired process records. */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { applyTopicMigration } from "../extensions/knowledge-flow/topic-migration.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { validateCitationChanges, validateRetirementReferences } from "../extensions/knowledge-flow/citation-retirement.js";
import { citationMarkers } from "../extensions/knowledge-flow/citation-retirement.js";
import type { TopicMigration } from "../extensions/knowledge-flow/topic-revision-types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { readState } from "../src/utils/state.js";
import { topicFiles, topicFixture, topicRecord } from "./knowledge-flow-topic-fixtures.js";

const pageId = "concepts/publishing";
const url = "https://github.com/example/wiki/pull/7";
const durable = "按序列化 UTF-8 总字节限制输入，避免中文与封装开销造成无效重试。";
const old = "PR #7 当前有三个 P2，先由另一台机器提交。";

async function retirementFixture() {
  const config = await topicFixture();
  await writeFile(path.join(config.wikiRoot, "wiki", `${pageId}.md`),
    "---\ntitle: Publishing\nprojectId: companion\n---\n\n## Publishing\n");
  return config;
}

async function migrationFixture() {
  const first = topicRecord("a", [old], { targetPageId: pageId });
  const second = topicRecord("c", [durable], { targetPageId: pageId });
  second.payload.basisRecordIds = [first.id];
  const records = [first, second]; const initial = await retirementFixture();
  await materializeRecords(initial, records);
  const previous = await readFile(path.join(initial.wikiRoot, "wiki", `${pageId}.md`), "utf8");
  const [retired, retained] = citationMarkers(previous);
  const migration: TopicMigration = { version: 1, basisRecordIds: records.map(item => item.id), pages: [{
    projectId: "companion", projectLabel: "Companion", pageId, title: "输入字节预算", topic: "样例素材推广",
    decisionObject: "样例项目首轮素材测试", topicId: "d".repeat(64),
    previousPages: [{ pageId, sha256: sha256Text(previous) }], body: `## 输入字节预算\n\n${durable} ${retained}\n`,
    citationRetirements: [{ citation: retired, reason: "移除已结束的临时协作，保留可复用的输入约束。", replacement: retained }],
  }] };
  return { records, previous, migration, initial, retired, retained };
}

describe("reviewed evidence retirement", () => {
  it("Given a named retirement, When rebuilt in either record order, Then only durable knowledge and sources remain", async () => {
    const { records, migration, initial, retained } = await migrationFixture();
    const left = { ...await retirementFixture(), topicMigration: migration };
    const right = { ...await retirementFixture(), topicMigration: migration };
    await materializeRecords(left, records); await materializeRecords(right, [...records].reverse());
    expect(await topicFiles(left)).toEqual(await topicFiles(right));
    const body = (await topicFiles(left)).get("publishing.md")!;
    expect(body).toContain(durable); expect(body).toContain(retained); expect(body).not.toContain(old);
    expect(parseFrontmatter(body).meta.knowledgePublicationRefs).toEqual([`${records[1].id}:0`]);
    const sources = await topicFiles(left, "sources"); expect(sources.size).toBe(1);
    for (const [name, content] of sources) expect(content).toBe((await topicFiles(initial, "sources")).get(name));
    const state = await readState(left.wikiRoot); expect(Object.keys(state.sources)).toEqual([...sources.keys()]);
    expect(await readFile(path.join(left.wikiRoot, "wiki/MOC.md"), "utf8")).not.toContain("三个 P2");
    const context = await buildHookContext({ config: left, prompt: "输入 UTF-8 字节预算", allowedPageIds: [pageId], seen: {} });
    expect(context.context).toContain("UTF-8"); expect(context.context).not.toContain("三个 P2");
  });

  it("Given a curated basis, When a later revision arrives, Then it applies without restoring retired evidence", async () => {
    const { records, migration } = await migrationFixture();
    const first = { ...await retirementFixture(), topicMigration: migration }; await materializeRecords(first, records);
    const previous = (await topicFiles(first)).get("publishing.md")!;
    const next = topicRecord("e", ["明确拒绝超限输入。"], { targetPageId: pageId });
    next.payload.basisRecordIds = records.map(item => item.id);
    next.payload.topicRevisions = [{ pageId, title: "输入字节预算", topic: "样例素材推广", decisionObject: "样例项目首轮素材测试",
      topicId: "d".repeat(64), basisHash: sha256Text(previous), claimIndexes: [0],
      body: `${parseFrontmatter(previous).body}\n明确拒绝超限输入。{{claim:0}}` }];
    const rebuilt = { ...await retirementFixture(), topicMigration: migration };
    expect((await materializeRecords(rebuilt, [...records, next])).conflicts).toEqual([]);
    expect((await topicFiles(rebuilt)).get("publishing.md")).toContain("明确拒绝超限输入");
    expect((await topicFiles(rebuilt, "sources")).size).toBe(2);
  });

  it("Given another page still citing the retired bundle, Then that page's unique source remains", async () => {
    const { records, migration, retired } = await migrationFixture();
    const config = { ...await retirementFixture(), topicMigration: migration };
    await writeFile(path.join(config.wikiRoot, "wiki/concepts/other.md"), `# Other\n\n独有上下文 ${retired}\n`);
    await materializeRecords(config, records);
    expect((await topicFiles(config, "sources")).size).toBe(2);
  });

  it("Given a frozen baseline source happens to match a generated name, Then retirement still preserves its original bytes", async () => {
    const { records, migration, initial } = await migrationFixture();
    const config = { ...await retirementFixture(), topicMigration: migration };
    await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
    const [name, content] = [...await topicFiles(initial, "sources")][0];
    await writeFile(path.join(config.wikiRoot, "sources", name), content);
    await materializeRecords(config, records);
    expect(await readFile(path.join(config.wikiRoot, "sources", name), "utf8")).toBe(content);
  });

  it("Given a changed frozen page or a silent dropped marker, Then nothing may authorize retirement", async () => {
    const { records, previous, migration } = await migrationFixture(); const config = await retirementFixture();
    expect(() => applyTopicMigration(config, migration, new Map([[pageId, `${previous}\nHuman note`]]), records)).toThrow(/hash/);
    const silent = structuredClone(migration); delete silent.pages[0].citationRetirements;
    expect(() => applyTopicMigration(config, silent, new Map([[pageId, previous]]), records)).toThrow(/citation/);
    expect((await topicFiles(config)).get("publishing.md")).toContain("## Publishing");
  });

  it("Given a pure process page and an exact review, When replayed, Then the page and its generated source leave the Wiki", async () => {
    const record = topicRecord("a", [old], { targetPageId: pageId }); const initial = await retirementFixture();
    await materializeRecords(initial, [record]); const previous = (await topicFiles(initial)).get("publishing.md")!;
    const migration: TopicMigration = { version: 1, basisRecordIds: [record.id], pages: [], retiredPages: [{
      projectId: "companion", pageId, sha256: sha256Text(previous), reason: "只有已结束的交接记录；过程可查原 PR。", externalReference: url }] };
    for (let run = 0; run < 2; run += 1) {
      const config = { ...await retirementFixture(), topicMigration: migration };
      await materializeRecords(config, [record]);
      expect(await topicFiles(config)).toEqual(new Map()); expect(await topicFiles(config, "sources")).toEqual(new Map());
      expect(await readFile(path.join(config.wikiRoot, "wiki/MOC.md"), "utf8")).not.toContain("publishing");
    }
  });

  it("Given an unrelated source file, Then a process-page retirement does not delete it", async () => {
    const { records, migration } = await migrationFixture(); const config = { ...await retirementFixture(), topicMigration: migration };
    await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
    await writeFile(path.join(config.wikiRoot, "sources/unique.md"), "未编译的独有材料");
    await materializeRecords(config, records);
    expect(await readFile(path.join(config.wikiRoot, "sources/unique.md"), "utf8")).toBe("未编译的独有材料");
  });

  it.each(["[[publishing]]", "[[concepts/publishing#Section|label]]", "[old](../concepts/publishing.md)"])(
    "Given a surviving link %s, Then whole-page retirement fails before changing the projection", async (link) => {
      const record = topicRecord("a", [old], { targetPageId: pageId }); const initial = await retirementFixture();
      await materializeRecords(initial, [record]); const previous = (await topicFiles(initial)).get("publishing.md")!;
      const migration: TopicMigration = { version: 1, basisRecordIds: [record.id], pages: [], retiredPages: [{
        projectId: "companion", pageId, sha256: sha256Text(previous), reason: "过程移交原 PR。", externalReference: url }] };
      const config = { ...await retirementFixture(), topicMigration: migration };
      await writeFile(path.join(config.wikiRoot, "wiki/concepts/other.md"), `# Other\n${link}\n`);
      await expect(materializeRecords(config, [record])).rejects.toThrow(/surviving Wiki link/);
      expect((await topicFiles(config)).get("publishing.md")).toContain("## Publishing");
      expect(await topicFiles(config, "sources")).toEqual(new Map());
    });

  it.each([
    { citation: "^[missing.md:1]", reason: "未知旧引用", replacement: "^[keep.md:2]" },
    { citation: "^[old.md:1]", reason: "", replacement: "^[keep.md:2]" },
    { citation: "^[old.md:1]", reason: "缺少承接", replacement: "^[missing.md:2]" },
  ])("Given an invalid retirement, Then replay rejects it (%j)", (item) => {
    expect(() => validateCitationChanges(["Old ^[old.md:1] Keep ^[keep.md:2]"], "Keep ^[keep.md:2]", [item])).toThrow(/retire|citation|replacement/);
  });

  it("Given a concatenated retirement replacement, When correction runs, Then the diagnostic names the invalid literal and allowed forms", () => {
    const replacement = "{{claim:0}}、{{claim:2}}";
    expect(() => validateCitationChanges(["Old ^[old.md:1]"], "Keep", [{
      citation: "^[old.md:1]", reason: "旧过程结束", replacement,
    }])).toThrow(new RegExp(`invalid replacement literal.*${replacement.replace(/[{}]/g, "\\$&")}`));
  });

  it("Given a valid replacement literal omitted from the revised body, When correction runs, Then the diagnostic names the missing survivor", () => {
    expect(() => validateCitationChanges(["Old ^[old.md:1]"], "Keep", [{
      citation: "^[old.md:1]", reason: "旧过程结束", replacement: "{{claim:0}}",
    }], { claimIndexes: [0] })).toThrow(/replacement literal.*not present in revised body/);
  });

  it("Given a guessed external record, Then model curation is rejected until original evidence supports it", () => {
    const item = { citation: "^[old.md:1]", reason: "已结束的过程", replacement: url };
    expect(() => validateRetirementReferences([item], ["PR #7"])).toThrow(/original evidence/);
    expect(() => validateRetirementReferences([item], [`见 ${url}`])).not.toThrow();
  });

});
