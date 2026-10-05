/** Ranking acceptance protects task evidence from weak matches and obsolete decisions. */
import { describe, expect, it } from "vitest";
import { buildViewerSnapshot } from "../src/viewer/snapshot.js";
import { rankDecisionSections } from "../src/context/task-sections.js";
import { GAME_PAGE, GAME_PROJECT, LANGUAGE_DECISION, useTaskWikiRoot } from "./fixtures/task-context-wiki.js";
import type { SemanticChunkHit } from "../src/context/retrieval.js";
import { cosineSimilarity } from "../src/utils/embeddings-search.js";

const wiki = useTaskWikiRoot("section-ranking");

/** Use current, source-backed page text; the test supplies only the external ranking signal. */
async function ranked(prompt: string, score?: number) {
  const pages = (await buildViewerSnapshot(wiki.value)).pages.filter(page => page.id === GAME_PAGE);
  const hits: SemanticChunkHit[] = score === undefined ? [] : [{ pageId: GAME_PAGE, slug: "game-runtime",
    text: pages[0].body, score, contentHash: "test-provider-hit" }];
  return rankDecisionSections(pages, prompt, hits);
}

describe("task evidence relevance", () => {
  it("Given identical embeddings round above one, When lexical terms differ, Then retains the strong semantic hit", async () => {
    const score = cosineSimilarity([1, 1, 1], [1, 1, 1]);
    const result = await ranked("Localization ownership", score);
    expect(result.map(section => section.heading)).toContain("当前语言范围");
  });

  it("Given only weak semantic neighbours, When weather is asked, Then supplies no game decision", async () => {
    expect(await ranked("明天的气温和降雨是多少？", 0.65)).toEqual([]);
  });

  it("Given shared generic words, When a different decision object is asked, Then abstains", async () => {
    expect(await ranked("继续推进项目，保持整体结构清晰，评估数据库索引迁移与消息队列架构。", 0.65)).toEqual([]);
  });

  it("Given a page-title match, When a short topic is asked, Then selects only sections with their own match", async () => {
    const result = await ranked("宿主");
    expect(result.map(section => section.heading)).toEqual(["当前语言范围"]);
  });

  it("Given a historical question, When a stronger rationale exists in another section, Then relevance still leads", async () => {
    const pages = (await buildViewerSnapshot(wiki.value)).pages.filter(page => page.id === GAME_PAGE);
    const hits = [{ pageId: GAME_PAGE, slug: "game-runtime", text: LANGUAGE_DECISION,
      score: 0.9, contentHash: "test-provider-hit" }];
    const result = rankDecisionSections(pages, "小游戏以前支持15种语言，为什么语言切换由宿主承担？", hits);
    expect(result[0]?.heading).toBe("当前语言范围");
  });

  it("Given a current rule and old implementation, When asking now, Then prioritizes the current rule", async () => {
    const result = await ranked("小游戏现在应该提供什么语言？");
    expect(result[0]?.heading).toContain("当前语言范围");
    expect(result.some(section => section.heading.endsWith("历史语言实现"))).toBe(false);
  });

  it("Given a current rule and old implementation, When asking about history, Then keeps the historical evidence", async () => {
    const result = await ranked("小游戏以前支持15种语言的历史实现是什么？");
    expect(result[0]?.heading).toContain("历史语言实现");
  });
});

describe("cross-project task evidence", () => {
  const LANGUAGE_QUESTION = "小游戏现在应该提供什么语言？";
  const unrelatedHit: SemanticChunkHit = { pageId: "concepts/unrelated", slug: "unrelated", text: "无关内容",
    score: 0.7, contentHash: "unrelated-hit" };
  const gamePages = async () => (await buildViewerSnapshot(wiki.value)).pages.filter(page => page.id === GAME_PAGE);

  it("Given semantic scope, When one word is asked, Then neither the owner nor another project gets topical retrieval", async () => {
    const pages = await gamePages();
    expect(rankDecisionSections(pages, "宿主", [], "other-project")).toEqual([]);
    expect(rankDecisionSections(pages, "宿主", [], GAME_PROJECT)).toEqual([]);
  });

  it("Given project scope, When one word is asked, Then the short topic still retrieves its own section", async () => {
    const result = rankDecisionSections(await gamePages(), "宿主", []);
    expect(result.map(section => section.heading)).toEqual(["当前语言范围"]);
  });

  it("Given embeddings are available, When another project's page matches only lexically, Then abstains", async () => {
    expect(rankDecisionSections(await gamePages(), LANGUAGE_QUESTION, [unrelatedHit], "other-project")).toEqual([]);
  });

  it("Given semantic agreement, When another project's page also matches several terms, Then keeps it", async () => {
    const hit = { ...unrelatedHit, pageId: GAME_PAGE, text: LANGUAGE_DECISION, score: 0.65 };
    const result = rankDecisionSections(await gamePages(), LANGUAGE_QUESTION, [hit], "other-project");
    expect(result[0]?.heading).toContain("当前语言范围");
  });

  it("Given no embeddings, When a broad question matches another project's page, Then the lexical fallback keeps it", async () => {
    const result = rankDecisionSections(await gamePages(), LANGUAGE_QUESTION, [], "other-project");
    expect(result[0]?.heading).toContain("当前语言范围");
  });
});
