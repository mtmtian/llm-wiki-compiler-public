/** Prepared reference metadata must describe complete evidence actually rendered by the hook. */
import { describe, expect, it } from "vitest";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { buildTaskContext } from "../src/context/task.js";
import { GAME_PAGE, GAME_PROJECT, LANGUAGE_QUESTION, seedTaskWiki, useTaskWikiRoot } from "./fixtures/task-context-wiki.js";

const wiki = useTaskWikiRoot("context-references");

/** Exercise the production renderer with real sources and an explicit output budget. */
function input(prompt = LANGUAGE_QUESTION, maxContextChars = 2400) {
  return { config: { wikiRoot: wiki.value, maxContextChars }, projectId: GAME_PROJECT,
    prompt, allowedPageIds: [GAME_PAGE], seen: {} };
}

describe("prepared hook references", () => {
  it("Given a sourced decision, When rendered, Then exposes its exact citation and current page revision", async () => {
    const result = await buildHookContext(input());
    expect(result.references).toEqual([expect.objectContaining({ pageId: GAME_PAGE,
      pageRevision: expect.stringMatching(/^[a-f0-9]{64}$/),
      citations: expect.arrayContaining(["^[language.md:1]", "^[scope.md:1]"]) })]);
    for (const reference of result.references) {
      for (const citation of reference.citations) expect(result.context).toContain(citation);
    }
  });

  it("Given only a pointer fits, When rendering, Then records no prepared evidence", async () => {
    const result = await buildHookContext(input(LANGUAGE_QUESTION, 350));
    expect(result.complete).toBe(false);
    expect(result.references).toEqual([]);
    expect(result.context).not.toContain("^[language.md:1]");
  });

  it("Given secondary sections were supplied before, When repeated, Then pointer summaries are not counted", async () => {
    const request = input("小游戏存档保留与当前语言范围分别怎么规定？");
    const first = await buildHookContext(request);
    expect(first.references.length).toBeGreaterThan(1);
    const repeated = await buildHookContext({ ...request, seen: first.seen });
    expect(repeated.context).toContain("已提供：");
    expect(repeated.references).toHaveLength(1);
    expect(repeated.references[0]).toEqual(first.references[0]);
  });
});

describe("temporal scope and no-hit boundaries", () => {
  it("Given an approved future change, When recalled, Then publication time does not promote it to currently effective", async () => {
    await seedTaskWiki(wiki.value, "已确认2026-10-01起小游戏改为中英双语，在此之前仍只提供英文。");
    const file = path.join(wiki.value, "wiki", GAME_PAGE + ".md");
    await writeFile(file, (await readFile(file, "utf8")).replace("## 当前语言范围", "## 已确认的语言安排"));
    const result = await buildTaskContext({ root: wiki.value, projectId: GAME_PROJECT,
      prompt: "已确认小游戏语言2026-10-01起改为中英双语的安排是什么？" });
    expect(result.evidence[0].temporalStatus).toBe("unspecified");
    expect(result.evidence[0].updatedAt).toBe("2026-09-18");
    expect(result.evidence[0].text).toContain("2026-10-01起");
    expect(result.evidence[0].sources.find(source => source.file === "language.md")?.text).toContain("在此之前仍只提供英文");
  });

  it("Given explicitly historical evidence, When recalled, Then preserves its status and labels it in the hook", async () => {
    const request = input("小游戏以前支持15种语言的历史实现是什么？");
    const task = await buildTaskContext({ root: wiki.value, projectId: GAME_PROJECT, prompt: request.prompt });
    expect(task.evidence[0].temporalStatus).toBe("historical");
    expect(task.evidence[0].updatedAt).toBe("2026-09-18");
    const hook = await buildHookContext(request);
    expect(hook.context).toContain("时间范围：历史资料");
    expect(hook.context).toContain("^[history.md:1]");
  });

  it("Given a global no-hit, When project context exists, Then follow-up pointers remain in that project", async () => {
    const result = await buildTaskContext({ root: wiki.value, projectId: GAME_PROJECT, scope: "semantic",
      prompt: "明天气温和降雨是多少？" });
    expect(result.evidence).toEqual([]);
    expect(result.followUpPageIds).toEqual([GAME_PAGE]);
  });

  it("Given a global no-hit without a project, When queried, Then recommends no arbitrary wiki pages", async () => {
    const result = await buildTaskContext({ root: wiki.value, scope: "semantic", prompt: "明天气温和降雨是多少？" });
    expect(result.evidence).toEqual([]);
    expect(result.followUpPageIds).toEqual([]);
  });
});
