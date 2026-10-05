/**
 * A reviewer-permitted source change settles a claim's authority: the program applies the shape the new source
 * permits instead of failing the batch, and disputes that survive the bounded correction stay human holds.
 */
import { describe, expect, it } from "vitest";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";
import { sha256Text } from "../src/connectors/hash.js";

const report = "助手报告：样例检查记录了三个重复文件，已按文件名排序。";
const question = "样例检查结果如何？";

function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `synthetic:${id}` };
}

function quoteId(request: Record<string, any>, evidenceId: string): string {
  return request.evidence.find((item: any) => item.id === evidenceId).quoteOptions[0].quoteId;
}

/** A first review that permits replacing claim 0's source, then whatever the scenario returns. */
function reviews(second: () => Record<string, unknown>) {
  let count = 0;
  return () => ++count === 1
    ? { ...accepted(), decision: "reject", reason: "主引文不是陈述该结果的记录", replaceEvidenceForClaims: [0],
      claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "应改用助手报告原文" }] }
    : second();
}

function page(body: string) { return [{ pageId, body, claimIds: ["c0"] }]; }

describe("authority shape of a permitted source change", () => {
  it("Given a historical fact bound to a user question, When the permitted correction moves it to the assistant report, Then it publishes as an attributed lesson", async () => {
    const input = job();
    const user = evidence("question", "user", question);
    const assistant = evidence("report", "assistant", report);
    input.evidence = [user, assistant];
    input.prompt = question;
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "样例检查记录了三个重复文件。", evidenceId: user.id, quote: user.text, kind: "fact", status: "historical" };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, assistant.id) },
        { field: "text", value: "助手当时报告样例检查记录了三个重复文件，本批未独立核验。" }] }], droppedClaimIds: [],
        pages: page("## 检查记录\n助手当时报告样例检查记录了三个重复文件，本批未独立核验。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改为归因报告。" }
      : initial, knowledge_topic_review: reviews(() => ({ ...accepted(), claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "归因明确" }] })) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    const source = result.contribution?.evidence.find(item => item.locator === assistant.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: source?.id, quote: report, kind: "lesson", status: "historical" });
  });

  it("Given assistant support inherited from a user primary, When the permitted correction moves the primary to an artifact, Then the support is dropped and the batch publishes", async () => {
    const input = job();
    const artifact = evidence("record", "artifact", "示例归档采用按名称排序。");
    const proposal = evidence("proposal", "assistant", "建议先排序，再检查重复文件。");
    input.evidence.push(artifact, proposal);
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "归档按名称排序。", kind: "fact", status: "historical",
      supportingQuotes: [{ evidenceId: proposal.id, quote: proposal.text }] };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, artifact.id) }] }], droppedClaimIds: [],
        pages: page("## 归档规则\n示例归档采用按名称排序。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改用资料原文。" }
      : initial, knowledge_topic_review: reviews(() => ({ ...accepted(), claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "资料支持" }] })) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims[0]).toMatchObject({ quote: artifact.text, kind: "fact", status: "historical" });
    expect(result.contribution?.claims[0].supportingQuotes ?? []).toEqual([]);
  });

  it("Given a decided decision whose only source is a user question, When the permitted correction moves it to the assistant report, Then it publishes as a historical lesson", async () => {
    const input = job();
    const user = evidence("question", "user", question);
    const assistant = evidence("report", "assistant", report);
    input.evidence = [user, assistant];
    input.prompt = question;
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "按文件名排序。", evidenceId: user.id, quote: user.text };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (value: any) => value.correction
      ? { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, assistant.id) }, { field: "status", value: "decided" },
        { field: "text", value: "助手当时报告已按文件名排序，本批未独立核验。" }] }], droppedClaimIds: [],
        pages: page("## 排序\n助手当时报告已按文件名排序，本批未独立核验。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改为归因报告。" }
      : initial, knowledge_topic_review: reviews(() => ({ ...accepted(), claimDecisions: [{ claimIndex: 0, decision: "accept", reason: "归因明确" }] })) });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result.status).toBe("submitted");
    expect(result.contribution?.claims[0]).toMatchObject({ quote: report, kind: "lesson", status: "historical" });
  });

  it("Given a correction with an unknown evidence reference, When validation fails after the bounded correction, Then it remains a technical failure", async () => {
    const input = job();
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], evidenceId: "missing", quote: "不存在的引文" };
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: initial,
      knowledge_topic_review: () => { throw new Error("review must not run on an invalid draft"); } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));

    expect(result).toMatchObject({ status: "error", retryable: false });
  });
});
