/** Acceptance tests for reviewed claims before page consolidation, relevance, supersession and sealed reads. */
import { describe, expect, it } from "vitest";
import { appendFile } from "node:fs/promises";
import path from "node:path";
import { buildTaskContext } from "../src/context/task.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { useTempRoot } from "./fixtures/temp-root.js";
import { CLAIM_REF, CLAIM_TEXT, reviewedClaim, writeReviewedClaims } from "./fixtures/reviewed-claims.js";
import { writePage } from "./fixtures/write-page.js";
import { writeSourceFile, writeSourceState, sha256Hex } from "./fixtures/state-json.js";

const root = useTempRoot();
const query = () => buildTaskContext({ root: root.dir, projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？", allowedPageIds: [] });

describe("reviewed claim task context", () => {
  it("Given a reviewed claim without a page, When querying its project, Then returns the claim and original quote", async () => {
    await writeReviewedClaims(root.dir);
    const result = await query();
    expect(result.evidence).toHaveLength(1);
    expect(result.evidence[0]).toMatchObject({ origin: "ledger", claimRef: CLAIM_REF, text: CLAIM_TEXT,
      qualifications: expect.stringContaining("仅适用于"), quotes: [{ quote: CLAIM_TEXT, kind: "user" }] });
    expect(result.evidence[0]).not.toHaveProperty("pageId");
  });

  it("Given foreign and unrelated claims, When querying the local task, Then only the relevant local claim survives", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim(), reviewedClaim({ claimRef: `${"b".repeat(64)}:0`, recordId: "b".repeat(64),
      projectId: "other-project", text: "FOREIGN_PRIVATE 小游戏存档保留" }), reviewedClaim({ claimRef: `${"c".repeat(64)}:0`,
      recordId: "c".repeat(64), title: "图像颜色", topic: "图像颜色", decisionObject: "图像颜色", text: "图像颜色采用蓝色" })]);
    const result = await query();
    expect(result.evidence.map(item => item.text)).toEqual([CLAIM_TEXT]);
    expect(JSON.stringify(result)).not.toContain("FOREIGN_PRIVATE");
  });

  it("Given a superseded claim, When asking current rules, Then never resurrects the old decision", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ superseded: true })]);
    expect((await query()).evidence).toEqual([]);
    const historical = await buildTaskContext({ root: root.dir, projectId: "sample-game", prompt: "以前小游戏存档保留规则是什么？" });
    expect(historical.evidence[0]).toMatchObject({ temporalStatus: "historical" });
  });

  it("Given modified sealed bytes, When retrieving, Then omits unverified claims and reports degradation", async () => {
    await writeReviewedClaims(root.dir);
    await appendFile(path.join(root.dir, ".llmwiki/reviewed-claims.json"), " ");
    const result = await query();
    expect(result.evidence).toEqual([]);
    expect(result.status).toBe("degraded");
    expect(result.diagnostics.warnings).toContain("reviewed-claims-invalid");
  });

  it("Given a sealed projection with an unsupported claim status, When retrieving, Then rejects its invalid contract", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ status: "not-reviewed" })]);
    const result = await query();
    expect(result.evidence).toEqual([]);
    expect(result.diagnostics.warnings).toContain("reviewed-claims-invalid");
  });

  it("Given a long first claim, When the hook budget cannot fit it, Then preserves a smaller claim and an expansion pointer", async () => {
    const long = reviewedClaim({ text: CLAIM_TEXT + "引用的完整适用条件不得被截断。".repeat(200) });
    const short = reviewedClaim({ recordId: "b".repeat(64), claimRef: `${"b".repeat(64)}:0` });
    await writeReviewedClaims(root.dir, [long, short]);
    const result = await buildHookContext({ config: { wikiRoot: root.dir, maxContextChars: 1400 },
      projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？", allowedPageIds: [], seen: {} });
    expect(result.context).toContain(`结论：${CLAIM_TEXT}\n`);
    expect(result.context).toContain(`补查条目（read_knowledge_claim）：${CLAIM_REF}`);
    expect(result.context).not.toContain("引用的完整适用条件");
    expect(result.references).toEqual([expect.objectContaining({ claimRef: short.claimRef })]);
    expect(result.complete).toBe(false);
    expect(result.context.length).toBeLessThanOrEqual(1400);
  });

  it("Given superseded page prose, When querying the current rule, Then only the new reviewed claim is offered", async () => {
    const previous = reviewedClaim({ text: "小游戏存档更新时可以清空。", targetPageId: "concepts/save-policy", superseded: true });
    const current = reviewedClaim({ recordId: "b".repeat(64), claimRef: `${"b".repeat(64)}:0` });
    await writeReviewedClaims(root.dir, [current], [previous]);
    await writeSourceFile(root.dir, "old.md", previous.text);
    await writeSourceState(root.dir, { "old.md": { hash: sha256Hex(previous.text), concepts: ["save-policy"] } });
    await writePage(path.join(root.dir, "wiki/concepts"), "save-policy", { title: "小游戏存档", projectId: "sample-game",
      knowledgePublicationRefs: [previous.claimRef] }, `## 当前规则\n${previous.text} ^[old.md:1]`);
    const result = await buildTaskContext({ root: root.dir, projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？" });
    expect(result.evidence.map(item => item.text)).toEqual([CLAIM_TEXT]);
    expect(result.evidence[0]).toHaveProperty("claimRef", current.claimRef);
  });

  it("Given an unrelated task, When only applicability mentions its terms, Then no claim body is injected", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ useWhen: "处理图像颜色时" })]);
    const result = await buildTaskContext({ root: root.dir, projectId: "sample-game", prompt: "图像颜色选择" });
    expect(result.evidence).toEqual([]);
    expect(JSON.stringify(result)).not.toContain(CLAIM_TEXT);
  });

  it("Given a retry republishes the same reviewed evidence, When retrieving, Then it occupies one context slot", async () => {
    const repeated = reviewedClaim({ claimRef: `${"b".repeat(64)}:0`, recordId: "b".repeat(64), recordedAt: "2026-09-02T00:00:00Z" });
    const qualified = reviewedClaim({ claimRef: `${"c".repeat(64)}:0`, recordId: "c".repeat(64), useWhen: "仅用于离线测试版本" });
    await writeReviewedClaims(root.dir, [reviewedClaim(), repeated, qualified]);
    const result = await query();
    expect(result.evidence).toHaveLength(2);
    expect(result.evidence.map(item => item.qualifications)).toEqual(expect.arrayContaining([
      expect.stringContaining("样例小游戏更新"), expect.stringContaining("离线测试版本"),
    ]));
  });

  it.each([
    { text: "合并部署后清理收尾", topic: "发布流程", prompt: "合并部署后清理收尾", count: 0 },
    { text: CLAIM_TEXT, topic: "小游戏存档", prompt: "小游戏更新怎么保留玩家存档", count: 1 },
  ])("Given another project's $topic, When querying $prompt, Then the domain gate admits $count claim", async ({ text, topic, prompt, count }) => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ projectId: "another-project", text, topic })]);
    const result = await buildTaskContext({ root: root.dir, projectId: "sample-game", scope: "semantic", prompt });
    expect(result.evidence).toHaveLength(count);
    if (count) expect(result.evidence[0].sourceProjectIds).toEqual(["another-project"]);
  });
});
