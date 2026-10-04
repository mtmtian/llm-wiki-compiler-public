/** Given/When/Then checks for quote suggestions routed through a stable-ID correction and full review. */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

function evidence(id: string, kind: FlowEvidence["kind"], text: string, observedAt: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt, locator: `turn:${id}` };
}

describe("reviewed quote repair", () => {
  it("Given a quote repair suggestion, When corrected, Then one patch edit and a fresh full review precede publication", async () => {
    const input = job();
    const earlier = evidence("earlier-user", "user", input.evidence[0].text, "2024-01-02T00:00:00Z");
    input.sessionContext!.evidence = [earlier];
    let quoteId = "";
    let edits = 0;
    let reviews = 0;
    const reviewedSources: string[] = [];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
      edits += 1;
      if (!request.correction) return draft();
      expect(request.correction.permissions.replaceEvidenceForClaimIds).toEqual(["c0"]);
      return { claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId }] }], droppedClaimIds: [],
        pages: [{ pageId, body: "## 当前结论\n预算 28 个虚构单位。{{claim:c0}}\n\n此前预算 12 个虚构单位。^[old.md:1]", claimIds: ["c0"] }],
        summary: "保留证据并修正措辞。" };
    }, knowledge_topic_review: (request: any) => {
      reviews += 1;
      reviewedSources.push(request.claims[0].evidenceId);
      if (reviews === 1) {
        const source = request.evidence.find((item: any) => item.id === earlier.id);
        quoteId = source.quoteOptions[0].quoteId;
        expect(request.evidence[0]).not.toHaveProperty("text");
        return { ...accepted(), decision: "reject", reason: "更正主引文绑定",
          claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "换到同文的早期原始来源" }],
          quoteRepairs: [{ claimIndex: 0, quoteId }] };
      }
      return accepted();
    } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect([edits, reviews]).toEqual([2, 2]);
    expect(reviewedSources).toEqual([input.evidence[0].id, earlier.id]);
    expect(result.claimReviews?.map(item => item.stage)).toEqual(["initial", "correction"]);
    const source = result.contribution?.evidence.find(item => item.locator === earlier.locator);
    expect(result.contribution?.claims[0]).toMatchObject({ evidenceId: source?.id, quote: input.evidence[0].text });
  });

  it("Given an older source and published page, When edit and review run, Then capture and publication dates remain distinct", async () => {
    const input = job();
    const assistant = evidence("assistant-analysis", "assistant", "建议预算为28个虚构单位。", "2022-03-04T00:00:00Z");
    input.sessionContext!.evidence = [assistant];
    const changed = draft();
    changed.claims[0].supportingQuotes = [{ evidenceId: assistant.id, quote: assistant.text }];
    const datedPage = original.replace("knowledgeTopicId:", "updatedAt: 2024-06-07T00:00:00Z\nknowledgeTopicId:");
    let editDate: unknown;
    let reviewDate: unknown;
    let reviewEvidence: any[] = [];
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => {
      editDate = request.pages[0].pagePublishedAt;
      return changed;
    }, knowledge_topic_review: (request: any) => {
      reviewDate = request.pages[0].pagePublishedAt;
      reviewEvidence = request.evidence;
      return accepted();
    } });

    const result = await consolidateSession(input, runtime, new Map([[pageId, datedPage]]));
    expect(result.status).toBe("submitted");
    expect(editDate).toBe("2024-06-07T00:00:00.000Z");
    expect(reviewDate).toBe(editDate);
    expect(reviewEvidence.find(item => item.id === assistant.id).observedAt).toBe("2022-03-04T00:00:00Z");
    expect(reviewEvidence.find(item => item.id === assistant.id)).not.toHaveProperty("text");
    expect(result.contribution?.evidence.find(item => item.locator === assistant.locator)?.observedAt).toBe("2022-03-04T00:00:00Z");
  });
});
