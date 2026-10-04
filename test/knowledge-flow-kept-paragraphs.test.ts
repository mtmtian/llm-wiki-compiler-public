/**
 * Kept paragraphs: the editor sees an existing page as numbered paragraphs and keeps unchanged ones by
 * placeholder. The program restores each kept paragraph's exact text, citation markers included, before
 * repair, validation and review; any placeholder it cannot restore becomes ordinary validation feedback.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { pageParagraphs, unexpandedPlaceholder, withKeptParagraphs } from "../extensions/knowledge-flow/kept-paragraphs.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const keptHistory = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n{{keep:P1}}";
const plannedPage = { pageId, original } as any;

function drafted(body: string) {
  const value = draft();
  value.pages[0].body = body;
  return value;
}

describe("kept paragraphs", () => {
  it("Given an existing page, When drafting, Then the editor sees its body as numbered paragraphs", async () => {
    let pages: any;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: accepted(),
      knowledge_topic_edit: (request: any) => { pages = request.pages; return draft(); } });
    await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(pages[0].originalParagraphs).toEqual([{ keep: "{{keep:P1}}", text: "当前模拟预算 12 个虚构单位。^[old.md:1]" }]);
    expect(pages[0]).not.toHaveProperty("original");
  });

  it("Given a draft that keeps the cited paragraph, When consolidated, Then review and publication see the exact original", async () => {
    let reviewed = "";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: drafted(keptHistory),
      knowledge_topic_review: (request: any) => { reviewed = request.revisions[0].body; return accepted(); } });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    const expected = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n\n当前模拟预算 12 个虚构单位。^[old.md:1]";
    expect(result.status).toBe("submitted");
    expect(reviewed).toBe(expected);
    expect(result.contribution?.topicRevisions?.[0].body).toBe(expected);
  });

  it("Given a first draft that dropped a marker, When the correction keeps that paragraph, Then it is submitted", async () => {
    const reworded = "## 当前结论\n预算 28 个虚构单位。{{claim:0}}\n\n## 决策历史\n此前预算约为 12 个虚构单位。";
    let reason = "";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: accepted(),
      knowledge_topic_edit: (request: any) => {
        if (!request.correction) return drafted(reworded);
        reason = request.correction.diagnostics;
        return drafted(keptHistory);
      } });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(reason).toContain("^[old.md:1]");
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions?.[0].body).toContain("当前模拟预算 12 个虚构单位。^[old.md:1]");
  });

  it("Given an inline placeholder in both attempts, When consolidated, Then the correction names it and the batch is held", async () => {
    const inline = keptHistory.replace("{{keep:P1}}", "此前预算见 {{keep:P1}}。");
    let reason = "";
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: accepted(),
      knowledge_topic_edit: (request: any) => { reason = request.correction?.diagnostics ?? reason; return drafted(inline); } });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(reason).toContain("kept paragraph placeholder {{keep:P1}} was not expanded");
    expect(result.status).toBe("needs_review");
    expect(result.error).toContain("{{keep:P1}}");
  });

  it("Given a repeated or out-of-range placeholder line, Then it keeps nothing and is dropped", () => {
    const body = withKeptParagraphs(drafted("{{keep:P1}}\n{{keep:P1}}\n{{keep:P2}}"), [plannedPage]).pages[0].body;
    expect(body).toBe("当前模拟预算 12 个虚构单位。^[old.md:1]");
    expect(unexpandedPlaceholder(body)).toBeUndefined();
  });

  it("Given a placeholder inside other text or on a new page, Then it stays literal for validation to reject", () => {
    const inline = withKeptParagraphs(drafted("见 {{keep:P1}}。"), [plannedPage]).pages[0].body;
    expect(unexpandedPlaceholder(inline)).toBe("{{keep:P1}}");
    const created = withKeptParagraphs(drafted("{{keep:P1}}"), [{ pageId, original: null } as any]).pages[0].body;
    expect(unexpandedPlaceholder(created)).toBe("{{keep:P1}}");
  });

  it("Given placeholders on adjacent lines, Then kept paragraphs stay apart from each other and from new text", () => {
    const page = { pageId, original: "---\ntitle: T\n---\n\n第一段。^[a.md:1]\n\n第二段。^[a.md:2]\n" } as any;
    const adjacent = withKeptParagraphs(drafted("{{keep:P1}}\n{{keep:P2}}\n新增一句。"), [page]).pages[0].body;
    expect(adjacent).toBe("第一段。^[a.md:1]\n\n第二段。^[a.md:2]\n\n新增一句。");
    const spaced = withKeptParagraphs(drafted("新增一句。\n\n{{keep:P2}}\n"), [page]).pages[0].body;
    expect(spaced).toBe("新增一句。\n\n第二段。^[a.md:2]\n");
  });

  it("Given a fenced code block with blank lines, Then it stays one paragraph with its exact text", () => {
    const page = "---\ntitle: T\n---\n\n## 配置\n\n```yaml\na: 1\n\nb: 2\n```\n\n\n结尾说明。^[x.md:1]\n";
    expect(pageParagraphs(page)).toEqual(["## 配置", "```yaml\na: 1\n\nb: 2\n```", "结尾说明。^[x.md:1]"]);
  });
});
