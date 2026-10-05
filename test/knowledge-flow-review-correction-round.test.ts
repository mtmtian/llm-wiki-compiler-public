/**
 * A validator-driven correction must not consume the reviewer's only chance to request a fix: after the
 * editor repairs a draft the program rejected, the reviewer's first verdict may still permit one more
 * correction, and a rejection after that reviewer-driven correction is final.
 */
import { describe, expect, it } from "vitest";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";
import { sha256Text } from "../src/connectors/hash.js";

const report = "助手报告：样例导入已改为按文件名排序。";
const question = "样例导入现在怎么排序？";

function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `synthetic:${id}` };
}

function quoteId(request: Record<string, any>, evidenceId: string): string {
  return request.evidence.find((item: any) => item.id === evidenceId).quoteOptions[0].quoteId;
}

const bodyWithMarker = "## 排序\n助手当时报告样例导入已改为按文件名排序，本批未独立核验。{{claim:c0}}\n\n此前内容。^[old.md:1]";

/** Initial draft whose page omits the claim marker, so program validation fails before any review. */
function invalidInitialDraft(user: FlowEvidence) {
  const initial = draft();
  initial.claims[0] = { ...initial.claims[0], text: "样例导入已改为按文件名排序。", evidenceId: user.id, quote: user.text, kind: "fact", status: "historical" };
  initial.pages[0].body = "## 排序\n样例导入已改为按文件名排序。\n\n此前内容。^[old.md:1]";
  return initial;
}

/** The editor repairs the marker first; only a reviewer-driven correction may move the source to the report. */
function editor(initial: ReturnType<typeof draft>, assistant: FlowEvidence) {
  return (value: any) => {
    if (!value.correction) return initial;
    if (!value.correction.review) return { claimUpdates: [], droppedClaimIds: [],
      pages: [{ pageId, body: "## 排序\n样例导入已改为按文件名排序。{{claim:c0}}\n\n此前内容。^[old.md:1]", claimIds: ["c0"] }], summary: "补上标记。" };
    return { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, assistant.id) },
      { field: "text", value: "助手当时报告样例导入已改为按文件名排序，本批未独立核验。" }] }], droppedClaimIds: [],
      pages: [{ pageId, body: bodyWithMarker, claimIds: ["c0"] }], summary: "按审核改用报告原文。" };
  };
}

const rejectWithPermission = { ...accepted(), decision: "reject", reason: "主引文不是陈述该结果的记录",
  replaceEvidenceForClaims: [0], claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "应改用助手报告原文" }] };

function scenario(secondVerdict: Record<string, unknown>) {
  const input = job();
  const user = evidence("question", "user", question);
  const assistant = evidence("report", "assistant", report);
  input.evidence = [user, assistant];
  input.prompt = question;
  const calls: string[] = [];
  let reviews = 0;
  const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: editor(invalidInitialDraft(user), assistant),
    knowledge_topic_review: () => (++reviews === 1 ? rejectWithPermission : secondVerdict) }, (_system, name) => calls.push(name));
  return { input, runtime, calls, assistant };
}

describe("reviewer-driven correction after a validator-driven one", () => {
  it("Given the only correction so far repaired validation, When the reviewer permits a source change, Then the editor gets that correction and publishes", async () => {
    const { input, runtime, calls, assistant } = scenario({ ...accepted(), claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "归因明确" }] });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims[0]).toMatchObject({ quote: assistant.text, kind: "lesson", status: "historical" });
    expect(calls).toEqual(["knowledge_topic_plan", "knowledge_topic_edit", "knowledge_topic_edit", "knowledge_topic_review",
      "knowledge_topic_edit", "knowledge_topic_review"]);
    expect(result.claimReviews!.map(item => item.stage)).toEqual(["correction", "recorrection"]);
  });

  it("Given the reviewer rejects the reviewer-driven correction too, Then the rejection is final without a third edit", async () => {
    const { input, runtime, calls } = scenario({ ...accepted(), decision: "reject", reason: "措辞仍是已验证事实",
      claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "仍写成已验证事实" }] });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result).toMatchObject({ status: "error", retryable: false });
    expect(calls.filter(name => name === "knowledge_topic_edit")).toHaveLength(3);
    expect(calls.filter(name => name === "knowledge_topic_review")).toHaveLength(2);
  });
});
