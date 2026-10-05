/**
 * Shared fixtures for source-correction tests in the knowledge-flow consolidation suite.
 *
 * These cases start with a claim grounded in a user question, then test how a permitted
 * reviewer correction changes the claim's evidence, text, classification, and publication.
 * The helpers centralize only repeated setup and verdict construction; each test retains its
 * own editor response and assertions for the behavior it exercises.
 */
import { expect } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { FlowConfig, FlowEvidence, FlowJob, FlowResult } from "../extensions/knowledge-flow/types.js";
import { accepted, config as testConfig, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";
import { sha256Text } from "../src/connectors/hash.js";

type ClaimOverrides = Partial<TopicDraft["claims"][number]>;

export function evidence(id: string, kind: FlowEvidence["kind"], text: string): FlowEvidence {
  return { id, kind, text, sha256: sha256Text(text), observedAt: "2025-01-15T00:00:00Z", locator: `synthetic:${id}` };
}

/** Read a source's first prepared quote option by its evidence identity. */
export function quoteId(request: Record<string, any>, evidenceId: string): string {
  return request.evidence.find((item: any) => item.id === evidenceId).quoteOptions[0].quoteId;
}

/** Build a question-backed initial claim and the assistant report that can replace its source. */
export function assistantQuestionScenario(question: string, report: string, initialClaimText: string,
  claimOverrides: ClaimOverrides = {}) {
  const input = job();
  const user = evidence("question", "user", question);
  const assistant = evidence("report", "assistant", report);
  input.evidence = [user, assistant];
  input.prompt = question;
  const initial = draft();
  initial.claims[0] = { ...initial.claims[0], text: initialClaimText, evidenceId: user.id, quote: user.text, ...claimOverrides };
  return { input, user, assistant, initial };
}

/** Return the accepting verdict used after a permitted source correction. */
export function acceptedClaimReview(reason: string) {
  return { ...accepted(), claimDecisions: [{ claimIndex: 0, decision: "accept", reason }] };
}

/** Return a reviewer that permits one source replacement before delegating to the scenario's verdict. */
export function reviewAfterPermittedSourceChange(second: () => Record<string, unknown>) {
  let count = 0;
  return () => ++count === 1
    ? { ...accepted(), decision: "reject", reason: "主引文不是陈述该结果的记录", replaceEvidenceForClaims: [0],
      claimDecisions: [{ claimIndex: 0, decision: "reject", reason: "应改用助手报告原文" }] }
    : second();
}

/** Configure the shared plan and two-stage review around a scenario-specific corrected draft. */
export function permittedSourceCorrectionRuntime(initial: TopicDraft, correction: (request: any) => unknown,
  acceptanceReason = "归因明确"): FlowConfig {
  return testConfig({ knowledge_topic_plan: plan(), knowledge_topic_edit: (request: any) => request.correction
    ? correction(request) : initial,
    knowledge_topic_review: reviewAfterPermittedSourceChange(() => acceptedClaimReview(acceptanceReason)) });
}

/** Run the shared page fixture and assert that the scenario publishes successfully. */
export async function consolidateSubmittedScenario(input: FlowJob, runtime: FlowConfig): Promise<FlowResult> {
  const result = await consolidateSession(input, runtime, new Map([[pageId, original]]));
  expect(result.status).toBe("submitted");
  return result;
}

/** Assert the scenario's resulting claim retains the expected published fields. */
export function expectClaimShape(result: FlowResult, expected: Record<string, unknown>): void {
  expect(result.contribution?.claims[0]).toMatchObject(expected);
}
