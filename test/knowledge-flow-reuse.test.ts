/** User-observable reuse scenarios exercise real pages, citations and fallback retrieval. */
import { describe, expect, it } from "vitest";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { GAME_PAGE, LANGUAGE_DECISION, LANGUAGE_QUESTION, useTaskWikiRoot, seedTaskWiki } from "./fixtures/task-context-wiki.js";

const taskWiki = useTaskWikiRoot("hook-reuse");

function input(prompt = LANGUAGE_QUESTION, seen: Record<string, string> = {}) {
  return { config: { wikiRoot: taskWiki.value, maxContextChars: 1500 }, prompt,
    allowedPageIds: [GAME_PAGE], seen, operationalContext: "OLD_RUNTIME: writer=old-machine" };
}

describe("decision reuse rather than any nonempty output", () => {
  it("Given a multi-decision page, When language is asked, Then supplies the current decision and its own source", async () => {
    const result = await buildHookContext(input());
    expect(result.context).toContain(LANGUAGE_DECISION);
    expect(result.context).toContain("language.md:1");
    expect(result.context).toContain("仅适用于当前小游戏");
    expect(result.context).not.toContain("OLD_RUNTIME");
    expect(result.context).not.toContain("OTHER_PROJECT_PRIVATE");
    expect(result.context.length).toBeLessThanOrEqual(1500);
  });

  it("Given a page-level scope qualifier, When a specific decision is selected, Then keeps that qualifier with its citation", async () => {
    const result = await buildHookContext(input());
    expect(result.context).toContain("仅适用于这一个小游戏，不能推广到其他游戏");
    expect(result.context).toContain("scope.md:1");
  });

  it("Given an introductory historical qualification, When a later decision is selected, Then does not drop that qualification", async () => {
    const file = path.join(taskWiki.value, "wiki", GAME_PAGE + ".md");
    const text = await readFile(file, "utf8");
    await writeFile(file, text.replace("## 适用范围", "本页只是历史分析，不表示接入已经完成。\n\n## 适用范围"));
    const result = await buildHookContext(input());
    expect(result.context).toContain(LANGUAGE_DECISION);
    expect(result.context).toContain("本页只是历史分析，不表示接入已经完成");
  });

  it("Given an uncited section with a sourced page scope, When queried, Then scope citations do not legitimize the uncited claim", async () => {
    const file = path.join(taskWiki.value, "wiki", GAME_PAGE + ".md");
    await writeFile(file, (await readFile(file, "utf8")) + "\n## 奖励状态\n未经证实的自动发奖已经上线。\n");
    const result = await buildHookContext(input("奖励状态自动发奖已经上线了吗？"));
    expect(result.context).not.toContain("未经证实的自动发奖已经上线");
  });

  it("Given the storage decision was supplied, When the question changes to language, Then supplies language", async () => {
    const first = await buildHookContext(input("更新小游戏时如何保留玩家存档？"));
    expect(first.context).toContain("保留玩家存档");
    const second = await buildHookContext(input(LANGUAGE_QUESTION, first.seen));
    expect(second.context).toContain(LANGUAGE_DECISION);
  });

  it("Given a later decision changed, When asking again, Then returns the new decision", async () => {
    const first = await buildHookContext(input());
    await seedTaskWiki(taskWiki.value, "当前小游戏改为中英双语，以满足新的中文试玩需求。");
    const result = await buildHookContext(input(LANGUAGE_QUESTION, first.seen));
    expect(result.context).toContain("改为中英双语");
    expect(result.context).not.toContain(LANGUAGE_DECISION);
  });

  it("Given semantic retrieval is unavailable, When Chinese is asked, Then uses lexical evidence and reports degradation", async () => {
    const result = await buildHookContext(input());
    expect(result.context).toContain(LANGUAGE_DECISION);
    expect(result.context).toMatch(/降级|语义检索不可用/);
    expect(result.context).not.toContain("OLD_RUNTIME");
  });

  it("Given an unrelated question, When no decision matches, Then does not substitute operational prose", async () => {
    const result = await buildHookContext(input("明天的气温和降雨是多少？"));
    expect(result.context).not.toContain("OLD_RUNTIME");
    expect(result.context).not.toContain(LANGUAGE_DECISION);
  });
});
