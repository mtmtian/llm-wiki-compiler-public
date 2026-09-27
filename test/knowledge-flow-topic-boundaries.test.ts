/** Topic routing failures must not change accepted prose or lose evidence relationships. */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { topicRecord, topicFixture, topicFiles } from "./knowledge-flow-topic-fixtures.js";

describe("topic materialization boundaries", () => {
  it("Given legacy related-page targets without decision objects, Then different topics stay separate during upgrade", async () => {
    const config = await topicFixture(); const goal = topicRecord("a", ["目标是应用注册"], { topic: "增长目标" });
    delete goal.payload.claims[0].decisionObject;
    const measurement = topicRecord("b", ["iOS 首次打开不等于归因安装"], { topic: "iOS 归因",
      targetPageId: `concepts/companion-c989edf1-record-${goal.id.slice(0, 32)}-0` });
    delete measurement.payload.claims[0].decisionObject; measurement.payload.basisRecordIds = [goal.id];
    const result = await materializeRecords(config, [goal, measurement]);
    expect(result).toEqual({ pages: 2, conflicts: [] });
    const pages = [...(await topicFiles(config)).values()];
    expect(pages.find(body => body.includes("目标是应用注册"))).not.toContain("iOS 首次打开不等于归因安装");
  });

  it("Given a configured baseline page without metadata, Then a reviewed explicit target preserves its identity", async () => {
    const config = await topicFixture(); const id = "concepts/existing";
    config.projects = { companion: { pages: [id] } };
    await writeFile(path.join(config.wikiRoot, "wiki", `${id}.md`), "---\ntitle: Existing title\n---\n\nOriginal context\n");
    const record = topicRecord("a", ["新增适用边界"], { targetPageId: id });
    const result = await materializeRecords(config, [record]);
    expect(result).toEqual({ pages: 1, conflicts: [] });
    const body = (await topicFiles(config)).get("existing.md")!;
    expect(body).toContain("title: Existing title"); expect(body).toContain("Original context");
    expect(body).toContain("knowledgeTopic: 样例素材推广"); expect(body).toContain("新增适用边界");
  });

  it("Given an explicit target for a different decision object, Then the contribution is held without rewriting the target", async () => {
    const config = await topicFixture();
    const baseline = "---\ntitle: Android\nprojectId: companion\nknowledgeTopic: 样例素材推广\nknowledgeDecisionObject: Android 安装\n---\n\nKeep Android separate\n";
    await writeFile(path.join(config.wikiRoot, "wiki/concepts/android.md"), baseline);
    const result = await materializeRecords(config, [topicRecord("a", ["iOS 视频访问"], { targetPageId: "concepts/android", decisionObject: "iOS 访问" })]);
    expect(result.conflicts).toHaveLength(1); expect(result.pages).toBe(1);
    expect((await topicFiles(config)).get("android.md")).toBe(baseline);
    expect((await topicFiles(config, "sources")).size).toBe(0);
  });

  it("Given two matching baseline destinations, Then ambiguous routing is held instead of choosing by filename", async () => {
    const config = await topicFixture();
    const body = "---\ntitle: Same scope\nprojectId: companion\nknowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试\n---\n\nPrior context\n";
    for (const name of ["one", "two"]) await writeFile(path.join(config.wikiRoot, `wiki/concepts/${name}.md`), body);
    const result = await materializeRecords(config, [topicRecord("a", ["不能猜测目标页"])]);
    expect(result.conflicts).toHaveLength(1); expect(result.pages).toBe(2);
    expect([...(await topicFiles(config)).values()]).toEqual([body, body]);
  });

  it("Given one publication about two topics, Then both pages cite one source whose live page mapping keeps both", async () => {
    const config = await topicFixture(); const record = topicRecord("a", ["样例素材推广第一段\n多行证据", "应用转化另行验证"]);
    record.payload.claims[1].topic = "应用转化"; record.payload.claims[1].decisionObject = "iOS 衡量";
    const result = await materializeRecords(config, [record]);
    expect(result).toEqual({ pages: 2, conflicts: [] });
    const sources = await topicFiles(config, "sources"); expect(sources.size).toBe(1);
    const state = JSON.parse(await readFile(path.join(config.wikiRoot, ".llmwiki/state.json"), "utf8"));
    expect(state.sources[[...sources.keys()][0]].concepts).toHaveLength(2);
    expect([...(await topicFiles(config)).values()].join("\n")).toMatch(/:\d+-\d+\]/);
  });

  it("Given independent same-object variants plus an unrelated accepted segment, Then only the unrelated evidence is visible", async () => {
    const config = await topicFixture(); const first = topicRecord("a", ["必须审批后上线", "稳定项目标识"]);
    first.payload.claims[1].topic = "项目身份"; first.payload.claims[1].decisionObject = "项目标识";
    const other = topicRecord("b", ["无须审批即可上线"]);
    const result = await materializeRecords(config, [first, other]);
    expect(result.conflicts).toHaveLength(1); expect(result.pages).toBe(1);
    const all = [...(await topicFiles(config)).values(), ...(await topicFiles(config, "sources")).values()].join("\n");
    expect(all).toContain("稳定项目标识"); expect(all).not.toContain("必须审批后上线"); expect(all).not.toContain("无须审批即可上线");
  });
});
