/**
 * Deterministic citation repair runs on each drafted page before validation and review. It only restores
 * provenance the draft visibly kept: a claim placeholder wrapped in marker syntax, a renumbered or merged
 * marker whose original ranges were dropped, and an unchanged line that lost its markers. Anything else
 * still reaches the strict citation validator unchanged.
 */
import { describe, expect, it } from "vitest";
import { citationChecklist, repairCitations, unaccountedCitations } from "../extensions/knowledge-flow/citation-repair.js";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const before = "---\ntitle: T\n---\n\n## 历史\n首轮预算 12 个单位。^[old.md:1]\n\n上线前完成两轮复盘。^[log.md:45-46]^[log.md:48-53]\n";

describe("deterministic citation repair", () => {
  it("Given a claim placeholder wrapped in marker syntax, Then it becomes the plain placeholder", () => {
    expect(repairCitations("新结论。^[{{claim:0}}]", before, [])).toBe("新结论。{{claim:0}}");
  });

  it("Given a merged marker over dropped original ranges, Then the original markers come back", () => {
    const body = "首轮预算 12 个单位。^[old.md:1]\n\n上线前完成两轮复盘。^[log.md:45-53]";
    expect(repairCitations(body, before, [])).toBe(
      "首轮预算 12 个单位。^[old.md:1]\n\n上线前完成两轮复盘。^[log.md:45-46]^[log.md:48-53]");
  });

  it("Given an unchanged line that lost its marker, Then the original line is restored", () => {
    const body = "## 当前结论\n新预算。{{claim:0}}\n\n## 历史\n首轮预算 12 个单位。\n\n上线前完成两轮复盘。^[log.md:45-46]^[log.md:48-53]";
    expect(repairCitations(body, before, [])).toContain("首轮预算 12 个单位。^[old.md:1]");
  });

  it("Given a reworded line, a retired marker or an unrelated new marker, Then nothing is changed", () => {
    const reworded = "首轮预算约 12 个单位。\n\n上线前完成两轮复盘。^[log.md:45-46]^[log.md:48-53]";
    expect(repairCitations(reworded, before, [])).toBe(reworded);
    const retired = "首轮预算 12 个单位。\n\n上线前完成两轮复盘。^[log.md:45-46]^[log.md:48-53]";
    expect(repairCitations(retired, before, ["^[old.md:1]"])).toBe(retired);
    const unrelated = "首轮预算 12 个单位。^[old.md:1]\n\n上线前完成两轮复盘。^[other.md:9]";
    expect(repairCitations(unrelated, before, [])).toBe(unrelated);
  });

  it("Given a new page without an original, Then the body is returned unchanged", () => {
    expect(repairCitations("新页。^[made-up.md:1]", null, [])).toBe("新页。^[made-up.md:1]");
  });

  it("Given a drafted page that kept the history line but lost its marker, When consolidated, Then it is submitted", async () => {
    const lost = draft();
    lost.pages[0].body = lost.pages[0].body.replace("此前预算 12 个虚构单位。^[old.md:1]", "当前模拟预算 12 个虚构单位。");
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_edit: lost, knowledge_topic_review: accepted() });
    const result = await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(result.contribution?.topicRevisions?.[0].body).toContain("当前模拟预算 12 个虚构单位。^[old.md:1]");
  });

  it("Given a previous draft, Then correction feedback names dropped, invented and outside-basis citations per page", () => {
    const previous = draft();
    previous.pages[0].body = "新结论。{{claim:0}}^[made-up.md:3]";
    previous.pages[0].citationRetirements = [{ citation: "^[elsewhere.md:1]", reason: "不相关", replacement: "{{claim:0}}" }];
    expect(unaccountedCitations(previous, plan().pages.map(() => ({ pageId, original } as any)))).toEqual([{ pageId,
      citations: ["^[old.md:1]"], invented: ["^[made-up.md:3]"], outsideBasis: ["^[elsewhere.md:1]"] }]);
    expect(citationChecklist([{ pageId, original } as any, { pageId: "concepts/new", original: null } as any]))
      .toEqual([{ pageId, existingCitations: ["^[old.md:1]"] }]);
  });

  it("Given an existing page, When drafting, Then the prompt lists the citations it must keep or retire", async () => {
    let checklist: unknown;
    const runtime = config({ knowledge_topic_plan: plan(), knowledge_topic_review: accepted(),
      knowledge_topic_edit: (request: any) => { checklist = request.citationChecklist; return draft(); } });
    await consolidateSession(job(), runtime, new Map([[pageId, original]]));
    expect(checklist).toEqual([{ pageId, existingCitations: ["^[old.md:1]"] }]);
  });
});
