/**
 * Cross-origin budget contracts after a held ledger claim is published by a later page retry.
 * Equivalence metadata never substitutes for a selected paragraph's complete text and sources.
 */
import path from "node:path";
import { describe, expect, it } from "vitest";
import { buildTaskContext } from "../src/context/task.js";
import { useTempRoot } from "./fixtures/temp-root.js";
import { CLAIM_TEXT, reviewedClaim, writeReviewedClaims } from "./fixtures/reviewed-claims.js";
import { writePage } from "./fixtures/write-page.js";
import { writeSourceFile, writeSourceState, sha256Hex } from "./fixtures/state-json.js";

const root = useTempRoot();
const PAGE_REF = `${"d".repeat(64)}:0`;
const USE_WHEN = "仅适用于样例小游戏更新";
const RATIONALE = "避免玩家进度丢失";
const query = () => buildTaskContext({ root: root.dir, projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？" });

/** Model an accepted retry page with real, hash-indexed source text. */
async function retryPage(options: { useWhen?: string; extra?: string; sourceText?: string; sourceSupplement?: string; missingSource?: boolean } = {}) {
  const sourceText = (options.sourceText ?? CLAIM_TEXT) + (options.sourceSupplement ? `\n${options.sourceSupplement}` : "");
  if (!options.missingSource) {
    await writeSourceFile(root.dir, "retry.md", sourceText);
    await writeSourceState(root.dir, { "retry.md": { hash: sha256Hex(sourceText), concepts: ["save-policy"] } });
  }
  await writePage(path.join(root.dir, "wiki/concepts"), "save-policy", { title: "小游戏存档", projectId: "sample-game",
    status: "decided", knowledgePublicationRefs: [PAGE_REF] },
  `## 当前存档规则\n${CLAIM_TEXT} ^[retry.md:1]\n\n适用条件：${options.useWhen ?? USE_WHEN}\n\n依据与取舍：${RATIONALE}\n\n${options.extra ?? ""}`);
}

/** Query overlapping retry evidence while requiring both provenance units. */
async function queryEquivalentRetry(additionalSource: string, pageExcerpt: string) {
  await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF] })]);
  await retryPage({ extra: pageExcerpt, sourceSupplement: additionalSource });
  const result = await query();
  expect(result.evidence).toHaveLength(2);
  return result;
}

describe("page and ledger evidence share one budget", () => {
  it("Given an equivalent published retry, When both origins match, Then the complete selected page spends one slot", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF] })]);
    await retryPage();
    const result = await query();
    expect(result.evidence).toHaveLength(1);
    expect(result.evidence[0]).toMatchObject({ pageId: "concepts/save-policy", text: expect.stringContaining(USE_WHEN) });
    expect(result.followUpClaimRefs).toEqual([]);
    expect(result.complete).toBe(true);
  });

  it("Given a later page with changed applicability, When retrieving, Then it cannot suppress the ledger condition", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF] })]);
    await retryPage({ useWhen: "仅用于离线测试版本" });
    const result = await query();
    expect(result.evidence).toHaveLength(2);
    expect(result.evidence.some(item => item.origin === "ledger" && item.qualifications.includes(USE_WHEN))).toBe(true);
  });

  it.each([{ missingSource: true }, { extra: "完整页面还保留其他存档知识。".repeat(1600) }])(
    "Given an unreadable or oversized page, When its claim is independently usable, Then the claim stays available: %j", async options => {
      await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF] })]);
      await retryPage(options);
      const result = await query();
      expect(result.evidence).toHaveLength(1);
      expect(result.evidence[0]).toMatchObject({ origin: "ledger", text: CLAIM_TEXT });
    });

  it("Given identical prose without an equivalence link, When retrieving, Then text similarity alone cannot merge evidence", async () => {
    await writeReviewedClaims(root.dir);
    await retryPage();
    expect((await query()).evidence).toHaveLength(2);
  });

  it("Given the page now cites different source bytes, When its stale equivalence link remains, Then the original claim survives", async () => {
    await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF] })]);
    await retryPage({ sourceText: "样例小游戏的图像缓存可以重建。" });
    const result = await query();
    expect(result.evidence).toHaveLength(2);
    expect(result.evidence.some(item => item.origin === "ledger" && item.quotes[0].quote === CLAIM_TEXT)).toBe(true);
  });

  it("Given a section retains another decision too, When overlap is only partial, Then both complete evidence units survive", async () => {
    const additional = "小游戏存档更新前必须完成备份。";
    const result = await queryEquivalentRetry(additional, `${additional} ^[retry.md:2]`);
    expect(result.evidence.some(item => item.origin !== "ledger" && item.text.includes(additional))).toBe(true);
  });

  it.each(["原始材料另有一条补充说明。", CLAIM_TEXT])(
    "Given equivalent prose has an additional cited source, When retrieving, Then its extra provenance stays available: %s", async additional => {
      const result = await queryEquivalentRetry(additional, "^[retry.md:2]");
      expect(result.evidence.some(item => item.origin !== "ledger"
        && item.sources.some(source => source.text === additional))).toBe(true);
    });

  it.each([false, true])("Given repeated ledger quotes, When page source multiplicity matches=%s, Then only matching evidence merges", async matches => {
    const claim = reviewedClaim();
    const quotes = [...claim.quotes, { ...claim.quotes[0], evidenceId: "user-2", locator: "turn:synthetic-2" }];
    await writeReviewedClaims(root.dir, [reviewedClaim({ equivalentPageRefs: [PAGE_REF], quotes })]);
    await retryPage(matches ? { extra: "^[retry.md:2]", sourceSupplement: CLAIM_TEXT } : {});
    const result = await query();
    expect(result.evidence).toHaveLength(matches ? 1 : 2);
    expect(result.evidence.find(item => item.origin !== "ledger")?.sources).toHaveLength(matches ? 2 : 1);
  });
});
