/**
 * Drafts often copy a passage with formatting removed, or cite the right passage under the wrong
 * evidence ID. One such claim used to send the whole batch into correction, where evidence for every
 * claim is re-chosen. Deterministic quote repair restores the exact original span (or the only
 * evidence that contains the quote) before validation; anything it cannot prove stays unchanged.
 * A correction is anchored to each claim's original evidence.
 */
import { describe, expect, it } from "vitest";
import { repairQuotes } from "../extensions/knowledge-flow/quote-repair.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const report = "## 结论\n\n- **周二发布**，发布前完成 `smoke` 回归。\n- 失败时回滚到上一版本。";
const evidence = (id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence =>
  ({ id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `codex://s/${id}` });
const items = [evidence("a1", "assistant", report), evidence("u1", "user", "按这个方案执行吧"),
  evidence("a2", "assistant", "另一份记录：先灰度 10% 再全量。")];
const claim = (evidenceId: string, quote: string) => ({ ...draft().claims[0], evidenceId, quote });

describe("deterministic quote repair", () => {
  it("Given a quote with markdown and spacing removed, Then the exact original span is restored", () => {
    const [repaired] = repairQuotes([claim("a1", "周二发布，发布前完成 smoke 回归。")], items);
    expect(repaired.quote).toBe("**周二发布**，发布前完成 `smoke` 回归。");
    expect(report.includes(repaired.quote)).toBe(true);
  });

  it("Given an exact quote under the wrong evidence ID, Then it is rebound to the only evidence containing it", () => {
    const [repaired] = repairQuotes([claim("a1", "先灰度 10% 再全量。")], items);
    expect([repaired.evidenceId, repaired.quote]).toEqual(["a2", "先灰度 10% 再全量。"]);
  });

  it("Given a supporting quote that lost formatting, Then it is repaired the same way", () => {
    const supported = { ...claim("u1", "按这个方案执行吧"), supportingQuotes: [{ evidenceId: "a1", quote: "周二发布，发布前完成 smoke 回归。" }] };
    expect(repairQuotes([supported], items)[0].supportingQuotes).toEqual([{ evidenceId: "a1", quote: "**周二发布**，发布前完成 `smoke` 回归。" }]);
  });

  it("Given a paraphrase, a stitched quote or an ambiguous one, Then the claim is left for the validator", () => {
    const unchanged = [claim("a1", "每周二上线并先跑回归"), claim("a1", "周二发布……回滚到上一版本"),
      claim("a1", "按这个方案执行吧，先灰度 10% 再全量。")];
    expect(repairQuotes(unchanged, items)).toEqual(unchanged);
  });

  it("Given one formatting-stripped quote in a draft, When consolidated, Then it publishes without a correction round", async () => {
    const formatted = "**样例素材测试预算**由12个虚构单位调整为28个虚构单位，其他条件不变。";
    const input = job();
    input.evidence[0] = evidence("user-2", "user", formatted);
    const stripped = draft();
    stripped.claims[0] = { ...stripped.claims[0], quote: "样例素材测试预算由12个虚构单位调整为28个虚构单位，其他条件不变。" };
    let edits = 0;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: accepted(),
      knowledge_topic_edit: () => { edits += 1; return stripped; } });
    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(edits).toBe(1);
    expect(result.contribution?.claims[0].quote).toBe(formatted);
  });

  it("Given a correction, Then each previous claim is anchored to quote options from its own evidence", async () => {
    const input = job();
    let anchors: unknown;
    let reviews = 0;
    const runtime = config({ knowledge_topic_plan: plan(),
      knowledge_topic_edit: (request: any) => { if (request.correction) anchors = request.correction.claimAnchors; return draft(); },
      knowledge_topic_review: () => reviews++ === 0 ? { ...accepted(), decision: "reject", reason: "请改写" } : accepted() });
    await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(anchors).toEqual([{ claimIndex: 0, evidenceId: "user-2", quoteIds: [expect.stringMatching(/^q-/)] }]);
  });
});
