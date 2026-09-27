/** Reviewed historical grouping is durable without changing immutable evidence. */
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { claimIdentity } from "../extensions/knowledge-flow/claim-identity.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { topicRecord, topicFixture, topicFiles } from "./knowledge-flow-topic-fixtures.js";

function legacyRecords() {
  const records = [topicRecord("a", ["先验证视频访问"], { topic: "增长目标", targetPageId: "concepts/old-hint" }),
    topicRecord("b", ["首轮预算为一百美元"], { topic: "测试预算" })];
  for (const record of records) delete record.payload.claims[0].decisionObject;
  return records;
}

function group(refs: string[]) {
  return { projectId: "companion", topic: "首轮获客测试", decisionObject: "样例项目首轮推广", claimRefs: refs };
}

describe("reviewed historical topic routes", () => {
  it("Given reviewed complementary legacy claims, Then one named topic retains their exact evidence and original identities", async () => {
    const config = await topicFixture(); const records = legacyRecords(); const original = JSON.stringify(records);
    config.topicRoutes = [group(records.map(record => `${record.id}:0`))];
    const result = await materializeRecords(config, records);
    expect(result.conflicts).toEqual([]); expect(result.pages).toBe(1);
    const [body] = (await topicFiles(config)).values(); const meta = parseFrontmatter(body).meta;
    expect(meta.title).toBe("首轮获客测试");
    for (const record of records) {
      expect(body).toContain(record.payload.claims[0].text);
      expect(meta.knowledgeClaimIds).toContain(claimIdentity("companion", record.payload.claims[0]));
    }
    expect((await topicFiles(config, "sources")).size).toBe(2);
    expect(JSON.stringify(records)).toBe(original);
  });

  it("Given a migrated topic, When a reviewed follow-up observes its records, Then the same page receives one more paragraph", async () => {
    const before = await topicFixture(); const after = await topicFixture(); const records = legacyRecords();
    before.topicRoutes = after.topicRoutes = [group(records.map(record => `${record.id}:0`))];
    await materializeRecords(before, records);
    const next = topicRecord("c", ["衡量应用内注册"], { topic: "首轮获客测试", decisionObject: "样例项目首轮推广" });
    next.payload.basisRecordIds = records.map(record => record.id);
    expect((await materializeRecords(after, [...records, next])).pages).toBe(1);
    expect([...(await topicFiles(after)).keys()]).toEqual([...(await topicFiles(before)).keys()]);
    expect([...(await topicFiles(after)).values()][0]).toContain("衡量应用内注册");
  });

  it("Given a jointly reviewed old group, When an unseen concurrent variant arrives, Then it is still held", async () => {
    const config = await topicFixture(); const records = legacyRecords();
    config.topicRoutes = [group(records.map(record => `${record.id}:0`))];
    const next = topicRecord("c", ["无需限制预算"], { topic: "首轮获客测试", decisionObject: "样例项目首轮推广" });
    const result = await materializeRecords(config, [...records, next]);
    expect(result.conflicts.flatMap(item => item.claimRefs)).toContain(`${next.id}:0`);
    expect([...(await topicFiles(config)).values()].join("\n")).not.toContain("无需限制预算");
  });

  it("Given identical reviewed routing, When order changes or replay repeats, Then page and evidence bytes stay identical", async () => {
    const left = await topicFixture(); const right = await topicFixture(); const records = legacyRecords();
    left.topicRoutes = [group(records.map(record => `${record.id}:0`))];
    right.topicRoutes = [group(records.map(record => `${record.id}:0`).reverse())];
    await materializeRecords(left, records); await materializeRecords(right, [...records].reverse());
    expect(await topicFiles(left)).toEqual(await topicFiles(right));
    expect(await topicFiles(left, "sources")).toEqual(await topicFiles(right, "sources"));
    left.topicRoutes = right.topicRoutes;
    await materializeRecords(left, records);
    expect(await topicFiles(left)).toEqual(await topicFiles(right));
  });

  it("Given invalid, duplicate, foreign or nonlegacy assignments, Then no topic page becomes visible", async () => {
    for (const variant of ["missing", "duplicate", "foreign", "new-contract"]) {
      const config = await topicFixture(); const records = legacyRecords();
      const route = group([`${records[0].id}:0`]);
      if (variant === "missing") route.claimRefs = ["f".repeat(64) + ":0"];
      if (variant === "duplicate") route.claimRefs.push(route.claimRefs[0]);
      if (variant === "foreign") route.projectId = "other";
      if (variant === "new-contract") records[0].payload.claims[0].decisionObject = "known object";
      config.topicRoutes = [route];
      await expect(materializeRecords(config, records)).rejects.toThrow(/route/i);
      expect((await topicFiles(config)).size).toBe(0);
    }
  });

  it("Given an existing stage, When its routing changes, Then it requires a fresh baseline before changing pages", async () => {
    const config = await topicFixture(); const records = legacyRecords();
    config.topicRoutes = [group(records.map(record => `${record.id}:0`))];
    await materializeRecords(config, records);
    const before = await topicFiles(config);
    config.topicRoutes[0].decisionObject = "重新审阅的对象";
    await expect(materializeRecords(config, records)).rejects.toThrow(/fresh baseline/i);
    expect(await topicFiles(config)).toEqual(before);
  });
});
