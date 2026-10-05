/**
 * A reviewer-permitted source change settles a claim's authority: the program applies the shape the new source
 * permits instead of failing the batch, and disputes that survive the bounded correction stay human holds.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";
import { assistantQuestionScenario, consolidateSubmittedScenario, evidence, expectClaimShape, permittedSourceCorrectionRuntime, quoteId } from "./knowledge-flow-source-correction-fixtures.js";

const report = "助手报告：样例检查记录了三个重复文件，已按文件名排序。";
const question = "样例检查结果如何？";

function page(body: string) { return [{ pageId, body, claimIds: ["c0"] }]; }

describe("authority shape of a permitted source change", () => {
  it("Given a historical fact bound to a user question, When the permitted correction moves it to the assistant report, Then it publishes as an attributed lesson", async () => {
    const { input, user, assistant, initial } = assistantQuestionScenario(question, report,
      "样例检查记录了三个重复文件。", { kind: "fact", status: "historical" });
    const runtime = permittedSourceCorrectionRuntime(initial, (value: any) => ({ claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, assistant.id) },
        { field: "text", value: "助手当时报告样例检查记录了三个重复文件，本批未独立核验。" }] }], droppedClaimIds: [],
        pages: page("## 检查记录\n助手当时报告样例检查记录了三个重复文件，本批未独立核验。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改为归因报告。" }));

    const result = await consolidateSubmittedScenario(input, runtime);

    const source = result.contribution?.evidence.find(item => item.locator === assistant.locator);
    expectClaimShape(result, { evidenceId: source?.id, quote: report, kind: "lesson", status: "historical" });
  });

  it("Given assistant support inherited from a user primary, When the permitted correction moves the primary to an artifact, Then the support is dropped and the batch publishes", async () => {
    const input = job();
    const artifact = evidence("record", "artifact", "示例归档采用按名称排序。");
    const proposal = evidence("proposal", "assistant", "建议先排序，再检查重复文件。");
    input.evidence.push(artifact, proposal);
    const initial = draft();
    initial.claims[0] = { ...initial.claims[0], text: "归档按名称排序。", kind: "fact", status: "historical",
      supportingQuotes: [{ evidenceId: proposal.id, quote: proposal.text }] };
    const runtime = permittedSourceCorrectionRuntime(initial, (value: any) => ({
      claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, artifact.id) }] }], droppedClaimIds: [],
      pages: page("## 归档规则\n示例归档采用按名称排序。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改用资料原文。" }), "资料支持");

    const result = await consolidateSubmittedScenario(input, runtime);

    expectClaimShape(result, { quote: artifact.text, kind: "fact", status: "historical" });
    expect(result.contribution?.claims[0].supportingQuotes ?? []).toEqual([]);
  });

  it("Given a decided decision whose only source is a user question, When the permitted correction moves it to the assistant report, Then it publishes as a historical lesson", async () => {
    const { input, assistant, initial } = assistantQuestionScenario(question, report, "按文件名排序。");
    const runtime = permittedSourceCorrectionRuntime(initial, (value: any) => ({ claimUpdates: [{ claimId: "c0", changes: [{ field: "quoteId", value: quoteId(value, assistant.id) }, { field: "status", value: "decided" },
        { field: "text", value: "助手当时报告已按文件名排序，本批未独立核验。" }] }], droppedClaimIds: [],
        pages: page("## 排序\n助手当时报告已按文件名排序，本批未独立核验。{{claim:c0}}\n\n此前内容。^[old.md:1]"), summary: "改为归因报告。" }));

    const result = await consolidateSubmittedScenario(input, runtime);

    expectClaimShape(result, { quote: report, kind: "lesson", status: "historical" });
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
