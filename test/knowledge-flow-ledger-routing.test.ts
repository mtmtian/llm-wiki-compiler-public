/**
 * With the knowledge ledger enabled, a held batch carries the claims its final review accepted
 * (deployment/KNOWLEDGE-LEDGER.md §7.2, step B3b). Only accepted claims and the evidence they cite
 * travel; the batch itself stays held, and every other outcome is unchanged. A held page is first retried
 * with its accepted claims alone (claim-pruning.ts); these scenarios hold that reduced page too.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowResult } from "../extensions/knowledge-flow/types.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const secondText = "样例素材每周复盘一次，复盘前不改预算。";

function twoClaimJob() {
  const input = job();
  input.evidence.push({ id: "user-3", kind: "user", text: secondText, sha256: sha256Text(secondText),
    observedAt: "2025-01-15T00:01:00Z", locator: "codex://s/t3" });
  return input;
}

function twoClaimDraft(): TopicDraft {
  const base = draft();
  const first = base.claims[0];
  const second = { ...first, text: secondText, evidenceId: "user-3", quote: secondText, title: "复盘节奏",
    slug: "sample-review-cadence", useWhen: "安排样例素材复盘时" };
  return { ...base, claims: [first, second], pages: [{ ...base.pages[0],
    body: `${base.pages[0].body}\n\n## 复盘\n每周复盘一次。{{claim:1}}`, claimIndexes: [0, 1] }] };
}

const verdict = (claimIndex: number, decision: string) => ({ claimIndex, decision, reason: `claim ${claimIndex} ${decision}` });
const held = (claimDecisions?: unknown[]) =>
  ({ ...accepted(), checkedClaimIndexes: [0, 1], decision: "needs_review", reason: "页面合并需人工确认",
    ...(claimDecisions ? { claimDecisions } : {}) });
/** Hold the two-claim page, then hold the page reduced to claim 0 while still accepting that claim. */
const heldTwice = (claimDecisions: unknown[]) => (request: any) => request.claims.length === 2 ? held(claimDecisions)
  : { ...accepted(), decision: "needs_review", reason: "精简后的页面仍需人工确认", claimDecisions: [verdict(0, "accept")] };

async function consolidate(review: unknown, knowledgeLedger = true): Promise<FlowResult> {
  const runtime = { ...config({ knowledge_topic_plan: plan(), knowledge_topic_edit: twoClaimDraft(), knowledge_topic_review: review }),
    knowledgeLedger };
  return consolidateSession(twoClaimJob(), runtime, new Map([[pageId, original]]));
}

describe("partial publication of accepted claims while the batch stays held", () => {
  it("Given an enabled ledger and a held page, Then only the accepted claim and its evidence travel with the hold", async () => {
    const result = await consolidate(heldTwice([verdict(0, "accept"), verdict(1, "reject")]));
    expect(result.status).toBe("needs_review");
    expect(result.contribution).toBeUndefined();
    expect(result.ledgerContribution?.claims.map(claim => claim.text)).toEqual([job().prompt]);
    expect(result.ledgerContribution?.evidence.map(item => item.text)).toEqual([job().prompt]);
    expect(result.error).toMatch(/页面合并需人工确认.*1 条.*账本/);
  });

  it("Given a disabled ledger, Then the held batch carries no ledger claims", async () => {
    const result = await consolidate(heldTwice([verdict(0, "accept"), verdict(1, "reject")]), false);
    expect(result.status).toBe("needs_review");
    expect(result.ledgerContribution).toBeUndefined();
  });

  it("Given incomplete conclusions or no accepted claim, Then nothing is published from the hold", async () => {
    for (const claimDecisions of [undefined, [verdict(0, "accept")], [verdict(0, "reject"), verdict(1, "needs_review")]]) {
      const result = await consolidate(held(claimDecisions));
      expect(result.status).toBe("needs_review");
      expect(result.ledgerContribution).toBeUndefined();
    }
  });

  it("Given an accepted page, Then the whole contribution is submitted as before even if a claim was rejected", async () => {
    const result = await consolidate({ ...accepted(), checkedClaimIndexes: [0, 1],
      claimDecisions: [verdict(0, "accept"), verdict(1, "reject")] });
    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims).toHaveLength(2);
    expect(result.ledgerContribution).toBeUndefined();
  });
});
