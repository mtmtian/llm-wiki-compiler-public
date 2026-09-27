/** Topic aggregation must preserve decision context, exact citations and project isolation. */
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { topicRecord, topicFixture, topicFiles } from "./knowledge-flow-topic-fixtures.js";

describe("publication topic pages", () => {
  it("Given several aspects of one decision, When materialized, Then one page and one evidence file retain every paragraph", async () => {
    const config = await topicFixture();
    const record = topicRecord("a", ["样例首轮以素材访问为目标", "样例实验只保留指定素材与版位", "注册转化列为后续模拟验证项"]);
    expect((await materializeRecords(config, [record])).pages).toBe(1);
    const pages = await topicFiles(config); const sources = await topicFiles(config, "sources");
    expect(sources.size).toBe(1);
    const [name, body] = [...pages][0];
    expect(name).not.toContain("record-");
    for (const claim of record.payload.claims) expect(body).toContain(claim.text);
    for (const match of body.matchAll(/\^\[([^:]+):(\d+)(?:-(\d+))?\]/g)) {
      const quoted = sources.get(match[1])!.split("\n").slice(Number(match[2]) - 1, Number(match[3] ?? match[2])).join("\n");
      expect(record.payload.claims.some(claim => claim.quote === quoted)).toBe(true);
    }
    expect([...body.matchAll(/\^\[/g)]).toHaveLength(3);
    expect(parseFrontmatter(body).meta.knowledgeClaimIds).toHaveLength(3);
  });

  it("Given a reviewed follow-up with a different slug, When replayed in either order, Then the same topic page is supplemented", async () => {
    const first = topicRecord("f", ["先验证内容访问"]);
    const next = topicRecord("a", ["有应用事件后再评估安装优化"], { slug: "app-measurement" });
    next.payload.basisRecordIds = [first.id];
    const left = await topicFixture(); const right = await topicFixture();
    expect((await materializeRecords(left, [first, next])).pages).toBe(1);
    expect((await materializeRecords(right, [next, first])).pages).toBe(1);
    expect(await topicFiles(left)).toEqual(await topicFiles(right));
    const before = await topicFiles(left);
    await materializeRecords(left, [next, first]);
    expect(await topicFiles(left)).toEqual(before);
  });

  it("Given an existing matching topic, When a publication omits its target, Then metadata matching reuses the page and preserves prose", async () => {
    const config = await topicFixture();
    const baseline = "---\ntitle: 投放决策\nprojectId: companion\nknowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试\nstatus: decided\n---\n\n## 背景\n\n人工写下的预算和目标。\n";
    await writeFile(path.join(config.wikiRoot, "wiki/concepts/promotion.md"), baseline);
    expect((await materializeRecords(config, [topicRecord("a", ["首轮先验证访问"])] )).pages).toBe(1);
    const pages = await topicFiles(config);
    expect(pages.has("promotion.md")).toBe(true);
    expect(pages.get("promotion.md")).toContain("人工写下的预算和目标。");
    expect(pages.get("promotion.md")).toContain("首轮先验证访问");
  });

  it("Given one topic but distinct decision objects or projects, Then those pages remain separate", async () => {
    const config = await topicFixture(); const first = topicRecord("a", ["iOS 先测访问"], { decisionObject: "iOS" });
    const android = topicRecord("b", ["Android 评估安装"], { decisionObject: "Android" });
    const other = topicRecord("c", ["其他产品独立测试"], { decisionObject: "iOS" });
    other.payload.projectId = "other";
    const result = await materializeRecords(config, [first, android, other]);
    expect(result.conflicts).toEqual([]); expect(result.pages).toBe(3);
  });

  it("Given a historical lesson supplementing a decided page, Then paragraph authority stays explicit and the decision remains decided", async () => {
    const config = await topicFixture(); const first = topicRecord("a", ["决定先验证样例素材访问表现"]);
    const analysis = topicRecord("b", ["历史建议：安装与视频传播应分开评估"], { kind: "lesson", status: "historical" });
    analysis.payload.basisRecordIds = [first.id]; analysis.payload.evidence[0].kind = "assistant";
    await materializeRecords(config, [first, analysis]);
    const [body] = (await topicFiles(config)).values();
    expect(parseFrontmatter(body).meta.status).toBe("decided");
    expect(body).toContain("historical"); expect(body).toContain("decided");
    expect(body).toContain("2026-09-17");
  });

  it("Given a legacy record target, When replayed from publications, Then it resolves to its topic instead of another fragment", async () => {
    const config = await topicFixture(); const first = topicRecord("f", ["先验证样例素材访问表现"]);
    const next = topicRecord("a", ["保留样例素材版位控制"], {
      topic: "旧标签名称", targetPageId: `concepts/companion-c989edf1-record-${first.id.slice(0, 32)}-0`,
    });
    next.payload.basisRecordIds = [first.id];
    expect((await materializeRecords(config, [next, first])).pages).toBe(1);
    const [body] = (await topicFiles(config)).values();
    expect(body).toContain("先验证样例素材访问表现"); expect(body).toContain("保留样例素材版位控制");
  });

  it("Given a foreign or missing target, Then no fallback page is created and its claim is held", async () => {
    const config = await topicFixture();
    await writeFile(path.join(config.wikiRoot, "wiki/concepts/foreign.md"), "---\ntitle: Other\nprojectId: other\n---\n\nOther project\n");
    for (const targetPageId of ["concepts/foreign", "concepts/missing"]) {
      const result = await materializeRecords(config, [topicRecord("a", ["不能写入其他项目"], { targetPageId })]);
      expect(result.conflicts).toHaveLength(1); expect(result.pages).toBe(1);
      expect((await topicFiles(config)).get("foreign.md")).not.toContain("不能写入其他项目");
    }
  });
});
