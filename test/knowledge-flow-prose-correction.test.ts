/**
 * An independent review can reject prose while retaining its evidence. Corrections must preserve
 * those references, receive every per-claim finding, and still pass a fresh whole-page review.
 * These cases exercise the actual consolidation boundary with scripted external model responses.
 */
import { expect, it } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowClaim, FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { applyReviewedQuoteRepairs } from "../extensions/knowledge-flow/reviewed-quote-repair.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { accepted, draft, job, original, pageId, plan, runConsolidation } from "./knowledge-flow-consolidation-fixtures.js";

const report = "助手分析：预算复核应保留原始输入和校验日志，以便重现计算口径差异。该分析尚未独立核验。";
const request = "继续处理。";
const finding = "原引文支持复核方法，但没有报告日期；将日期改为本批次捕获的助手分析。";
const initialText = `2025-01-15 的${report}`;
const correctedText = `本批次捕获的${report}`;

function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), locator: `synthetic:${id}`, observedAt: "2025-01-15T00:00:00Z" };
}

function reportDraft(text: string, source: FlowEvidence) {
  const result = draft();
  result.claims[0] = { ...result.claims[0], text, evidenceId: source.id, quote: source.text, kind: "lesson", status: "historical" };
  result.pages[0].body = `## 预算复核方法\n${text}{{claim:0}}\n\n此前预算 12 个虚构单位。^[old.md:1]`;
  return result;
}

async function correctionCase(retained: boolean, finalReject = false) {
  const assistant = evidence("report", "assistant", report);
  const user = evidence("request", "user", request);
  const input = { ...job(), evidence: [assistant, user], prompt: request };
  let reviews = 0;
  let correction: Record<string, unknown> | undefined;
  const reviewed: FlowClaim[][] = [];
  const result = await runConsolidation(input, { knowledge_topic_plan: plan(),
    knowledge_topic_edit: (value: { correction?: Record<string, unknown> }) => {
      correction = value.correction;
      return value.correction ? reportDraft(correctedText, user) : reportDraft(initialText, assistant);
    },
    knowledge_topic_review: (value: { claims: FlowClaim[] }) => {
      reviewed.push(value.claims);
      const rejected = reviews++ === 0 || finalReject;
      return { ...accepted(), decision: rejected ? "reject" : "accept", reason: rejected ? finding : "corrected prose and evidence checked",
        retainEvidenceForClaims: retained ? [0] : [],
        claimDecisions: [{ claimIndex: 0, decision: rejected ? "reject" : "accept", reason: finding }] };
    },
  }, new Map([[pageId, original]]));
  return { result, reviewed, correction };
}

it("Given a prose-only rejection, When the editor picks a user request, Then the historical report keeps its original citation", async () => {
  const { result, reviewed, correction } = await correctionCase(true);
  expect(result.status).toBe("submitted");
  expect(reviewed).toHaveLength(2);
  expect(reviewed[1][0]).toMatchObject({ text: correctedText, evidenceId: "report", quote: report, kind: "lesson", status: "historical" });
  expect(correction?.review).toMatchObject({ retainEvidenceForClaims: [0], claimDecisions: [{ claimIndex: 0, reason: finding }] });
});

it("Given a source rejection with no retention advice, When corrected, Then the selected source can change", async () => {
  const { reviewed } = await correctionCase(false);
  expect(reviewed[1][0]).toMatchObject({ evidenceId: "request", quote: request });
});

it("Given retained references but another rejected independent review, Then no knowledge is published", async () => {
  const { result, reviewed } = await correctionCase(true, true);
  expect(reviewed).toHaveLength(2);
  expect(result).toMatchObject({ status: "needs_review", publishedPageIds: [] });
  expect(result.contribution).toBeUndefined();
});

it("Given contradictory retain and replace advice, Then automatic quote replacement yields to full correction", () => {
  const first = evidence("first", "assistant", report);
  const second = evidence("second", "assistant", report);
  const catalog = buildCorrectionEvidence([first, second]);
  const review = { decision: "reject", retainEvidenceForClaims: [0],
    claimDecisions: [{ claimIndex: 0, decision: "reject" }],
    quoteRepairs: [{ claimIndex: 0, quoteId: catalog[1].quoteOptions[0].quoteId }] };
  expect(applyReviewedQuoteRepairs(reportDraft(correctedText, first), review, catalog, [first, second])).toBeNull();
});
