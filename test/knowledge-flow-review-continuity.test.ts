/** Reviewers must see prior findings and the exact scope of corrections without treating prior acceptance as evidence. */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan, runConsolidation } from "./knowledge-flow-consolidation-fixtures.js";

function rejection() {
  return { ...accepted(), decision: "reject", reason: "请明确当前预算的适用范围", claimDecisions: [
    { claimIndex: 0, decision: "reject", reason: "正文须写明样例素材测试" }] };
}

describe("review continuity", () => {
  it("Given a wording correction, Then an isolated reviewer receives prior findings, stable claim mapping and changed prose", async () => {
    const requests: any[] = []; const systems: string[] = [];
    const fixed = draft(); fixed.pages[0].body = fixed.pages[0].body.replace("预算 28", "样例素材测试预算 28");
    const runtime = config({ knowledge_topic_plan: plan(),
      knowledge_topic_edit: (request: any) => request.correction ? fixed : draft(),
      knowledge_topic_review: (request: any) => { requests.push(request); return requests.length === 1 ? rejection() : accepted(); }
    }, (system, name) => { if (name === "knowledge_topic_review") systems.push(system); });

    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    expect(requests[1].reviewContext).toMatchObject({ mode: "correction", previousReview: rejection(),
      claimMapping: [{ claimId: "c0", previousClaimIndex: 0, claimIndex: 0 }] });
    expect(requests[1].reviewContext.changedPages[0].addedParagraphs.join("\n")).toContain("样例素材测试预算 28");
    expect(systems[1]).toContain("Previously accepted claims are not evidence");
    expect(requests[1].priorSources["old.md"]).toBeTruthy();
  });

  it.each([0, 1])("Given only claim %i survives, Then subset review identifies dropped claims and remaps the current index", async keptIndex => {
    const initial = draft();
    initial.claims.push({ ...initial.claims[0], text: "其他条件不变。", title: "适用条件" });
    initial.pages[0].body += "\n\n其他条件不变。{{claim:1}}";
    initial.pages[0].claimIndexes = [0, 1];
    const rejected = { ...accepted(), decision: "reject", checkedClaimIndexes: [0, 1], claimDecisions: [
      { claimIndex: 0, decision: keptIndex === 0 ? "accept" : "reject", reason: "逐条按证据判定" },
      { claimIndex: 1, decision: keptIndex === 1 ? "accept" : "reject", reason: "逐条按证据判定" }] };
    const requests: any[] = [];
    const result = await runConsolidation(job(), { knowledge_topic_plan: plan(), knowledge_topic_edit: initial,
      knowledge_topic_review: (request: any) => { requests.push(request); return requests.length < 3 ? rejected : accepted(); }
    }, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims).toHaveLength(1);
    expect(requests[2].reviewContext).toMatchObject({ mode: "accepted_subset", previousReview: rejected,
      claimMapping: [{ claimId: "c0", previousClaimIndex: 0, claimIndex: keptIndex === 0 ? 0 : null },
        { claimId: "c1", previousClaimIndex: 1, claimIndex: keptIndex === 1 ? 0 : null }] });
    expect(requests[2].claims).toHaveLength(1);
    const changes = requests[2].reviewContext.changedPages[0];
    expect(changes.removedParagraphs.join("\n")).toContain(keptIndex === 0 ? "其他条件不变" : "预算 28");
    expect(changes.addedParagraphs).toEqual([]);
  });
});
