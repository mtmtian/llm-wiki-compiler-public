/** Given/When/Then contracts for topic identity and decision context. */
import { createHash } from "node:crypto";
import Ajv from "ajv";
import { describe, expect, it, vi } from "vitest";
import { extractClaims, validateClaims } from "../extensions/knowledge-flow/extract.js";
import { createCorrectionEditTool, createEditTool, createPlanTool, createTopicReviewTool, topicReviewTool } from "../extensions/knowledge-flow/consolidation-schema.js";
import { buildCorrectionEvidence } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { reviewClaims } from "../extensions/knowledge-flow/review.js";
import type { FlowClaim, FlowEvidence, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";

const text = "首轮先验证视频访问，再决定是否做安装优化";
const evidence: FlowEvidence = {
  id: "e1", kind: "user", text, locator: "turn:t1", observedAt: "2026-09-17T00:00:00Z",
  sha256: createHash("sha256").update(text).digest("hex"),
};
const job = {
  projectId: "companion", projectLabel: "Companion", prompt: text, lastAssistant: "",
  evidence: [evidence], allowedPageIds: ["concepts/sample-material-promotion"],
} as FlowJob;
const claim: FlowClaim = {
  text, evidenceId: evidence.id, quote: text, title: "样例素材推广", topic: "样例素材推广",
  decisionObject: "样例项目首轮素材测试", slug: "sample-material-promotion", targetPageId: null,
  kind: "decision", status: "decided", useWhen: "启动样例首轮素材推广时", rationale: "保留访问优先的取舍",
};

function providerReturning(value: unknown) {
  const toolCall = vi.fn<LLMProvider["toolCall"]>().mockResolvedValue(JSON.stringify(value));
  return { provider: { toolCall } as unknown as LLMProvider, toolCall };
}

/** A valid new topic lets schema tests change only the rejected field. */
function plannedTopic() {
  return { action: "create", targetPageId: null, title: "Topic", topic: "Topic", decisionObject: "Object", reason: "new topic" };
}

describe("knowledge-flow topic identity contract", () => {
  it("Given a topic claim, When extraction builds its tool schema, Then decisionObject is required and bounded", async () => {
    const { provider, toolCall } = providerReturning({ claims: [claim] });
    await extractClaims(provider, job, new Map(), 5);
    const schema = toolCall.mock.calls[0][2][0].input_schema as any;
    const item = schema.properties.claims.items;
    expect(item.required).toContain("decisionObject");
    expect(item.properties.decisionObject).toMatchObject({ type: "string", minLength: 1, maxLength: 160 });
  });

  it("Given a legacy claim without decisionObject, When replayed, Then it remains valid while valid metadata is retained", () => {
    const { decisionObject: _legacyField, ...legacyClaim } = claim;
    expect(validateClaims([legacyClaim], [evidence], [], 5)[0]).not.toHaveProperty("decisionObject");
    expect(validateClaims([{ ...claim, decisionObject: "  样例首轮素材推广  " }], [evidence], [], 5)[0]).toMatchObject({ decisionObject: "样例首轮素材推广" });
  });

  it.each([null, "", " ", 42, "x".repeat(161)])(
    "Given an explicit invalid decisionObject (%j), When normalized, Then the claim is rejected",
    (decisionObject) => {
      expect(validateClaims([{ ...claim, decisionObject }], [evidence], [], 5)).toEqual([]);
    },
  );

  it("Given existing pages, When extraction is prompted, Then matching and evidence-preserving routing instructions are explicit", async () => {
    const { provider, toolCall } = providerReturning({ claims: [claim] });
    await extractClaims(provider, job, new Map([["concepts/sample-material-promotion", "knowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试"]]), 5);
    const system = String(toolCall.mock.calls[0][0]);
    expect(system).toContain("same project");
    expect(system).toContain("canonical topic");
    expect(system).toContain("decisionObject");
    expect(system).toContain("reuse its targetPageId");
    expect(system).toContain("knowledgeTopic");
    expect(system).toContain("knowledgeDecisionObject");
    expect(system).toContain("complementary claims");
    expect(system).toContain("title/slug");
    expect(system).toContain("rationale");
    expect(system).toContain("useWhen");
    expect(system).toContain("tradeoffs");
    expect(system).toContain("the user does not need to repeat every parameter");
    expect(system).toContain("not independently verified in this batch");
    expect(system).toContain("Evidence origin=current means only that it arrived in this batch");
    expect(system).toContain("pagePublishedAt is the page frontmatter updatedAt value");
  });

  it("Given a candidate and existing pages, When review is prompted, Then ownership, identity and ambiguous routing gates are explicit", async () => {
    const { provider, toolCall } = providerReturning({ decisions: [{ index: 0, decision: "accept", reason: "supported", conflictingPageIds: [] }] });
    await reviewClaims(provider, job, [claim], new Map([["concepts/sample-material-promotion", "knowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试"]]));
    const system = String(toolCall.mock.calls[0][0]);
    expect(system).toContain("target page ownership");
    expect(system).toContain("topic/decisionObject");
    expect(system).toContain("legacy synonym");
    expect(system).toContain("different decision objects");
    expect(system).toContain("complementary claims");
    expect(system).toContain("ambiguous routing");
    expect(system).toContain("omitted existing matching page");
    expect(system).toContain("the user does not need to repeat every parameter");
    expect(system).toContain("not independently verified in this batch");
  });

  it("binds correction schemas to frozen evidence and page destinations", () => {
    const planSchema = createPlanTool(["concepts/sample-material-promotion"]).input_schema as any;
    expect(planSchema.properties.plan.anyOf[0].properties.pages.items.properties.targetPageId.anyOf[0].enum).toEqual(["concepts/sample-material-promotion"]);
    const editSchema = createEditTool(["concepts/sample-material-promotion"], ["e1"]).input_schema as any;
    expect(editSchema.properties.claims.items.properties.evidenceId.enum).toEqual(["e1"]);
    expect(editSchema.properties.pages.items.properties.pageId.enum).toEqual(["concepts/sample-material-promotion"]);
  });

  it("validates an empty catalog while refusing an invented destination", () => {
    const validate = new Ajv({ strict: false }).compile(createPlanTool([]).input_schema);
    const page = plannedTopic();
    const plan = { summary: "summary", disposition: "edit", reason: "new", pages: [page] };
    expect(validate({ plan })).toBe(true);
    expect(validate({ plan: { ...plan, pages: [{ ...page, targetPageId: "concepts/invented" }] } })).toBe(false);
  });

  it("Given a corrected plan, When no edit is intended, Then pages must be empty", () => {
    const validate = new Ajv({ strict: false }).compile(createPlanTool([]).input_schema);
    const page = plannedTopic();
    const plan = { summary: "summary", disposition: "needs_review", reason: "uncertain", pages: [] };
    expect(validate({ plan })).toBe(true);
    expect(validate({ plan: { ...plan, disposition: "noop" } })).toBe(true);
    expect(validate({ plan: { ...plan, pages: [page] } })).toBe(false);
    expect(validate({ plan: { ...plan, disposition: "edit" } })).toBe(false);
  });

  it("Given a topic review, When the model reports coverage, Then only actual claims, revisions and retirements are valid", () => {
    expect(topicReviewTool.name).toBe("knowledge_topic_review");
    const schema = createTopicReviewTool(1, ["concepts/sample-material-promotion"], ["^[old.md:1]"]).input_schema;
    const validate = new Ajv({ strict: false }).compile(schema);
    const review = { decision: "accept", reason: "supported", checkedClaimIndexes: [0],
      checkedPageIds: ["concepts/sample-material-promotion"], checkedRetiredCitations: ["^[old.md:1]"] };
    expect(validate(review)).toBe(true);
    expect(validate({ ...review, checkedPageIds: ["concepts/sample-material-promotion", "concepts/catalog"] })).toBe(false);
    expect(validate({ ...review, checkedClaimIndexes: [1] })).toBe(false);
    expect(validate({ ...review, checkedRetiredCitations: ["^[catalog.md:1]"] })).toBe(false);
  });

  it.each(["{{claim:0}}、{{claim:2}}", "本页规则由 {{claim:2}} 替代，且不再记录执行状态。"])(
    "Given a correction retirement replacement %s, When the correction tool validates it, Then explanatory or concatenated prose is rejected",
    (replacement) => {
      const evidenceText = "保留证据";
      const catalog = buildCorrectionEvidence([{ id: "e1", kind: "user", text: evidenceText,
        locator: "turn:e1", observedAt: "2026-09-21T00:00:00Z", sha256: createHash("sha256").update(evidenceText).digest("hex") }]);
      const editSchema = createCorrectionEditTool(["concepts/sample-material-promotion"], catalog).input_schema as any;
      const replacementSchema = editSchema.properties.pages.items.properties.citationRetirements.items.properties.replacement;
      const validate = new Ajv({ strict: false }).compile(replacementSchema);
      expect(validate(replacement)).toBe(false);
    },
  );
});
