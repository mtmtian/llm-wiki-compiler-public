/** Real on-disk decision pages used by hook and MCP reuse acceptance tests. */
import path from "node:path";
import { rm } from "node:fs/promises";
import { afterEach, beforeEach } from "vitest";
import { writePage } from "./write-page.js";
import { sha256Hex, writeSourceFile, writeSourceState } from "./state-json.js";
import { makeTempRoot } from "./temp-root.js";

export const GAME_PAGE = "concepts/game-runtime";
export const GAME_PROJECT = "game-demo";
export const LANGUAGE_QUESTION = "这个小游戏现在只做英文还是仍要15种语言？";
export const LANGUAGE_DECISION = "当前小游戏只提供英文版，因为语言切换由外层宿主承担。";

export interface TaskWikiRootHandle {
  value: string;
}

/** Create and seed a real decision wiki for hook tests with one shared lifecycle. */
export function useTaskWikiRoot(prefix: string): TaskWikiRootHandle {
  const handle: TaskWikiRootHandle = { value: "" };
  beforeEach(async () => {
    handle.value = await makeTempRoot(prefix);
    await seedTaskWiki(handle.value);
  });
  afterEach(async () => {
    await rm(handle.value, { recursive: true, force: true });
  });
  return handle;
}

/** Every decision has an independent source; the first source is deliberately unrelated. */
export async function seedTaskWiki(root: string, language = LANGUAGE_DECISION): Promise<void> {
  const sourceTexts = {
    "scope.md": "仅适用于这一个小游戏，不能推广到其他游戏。",
    "save.md": "更新必须沿用原游戏身份，保留玩家存档与已解锁关卡。",
    "language.md": language,
    "history.md": "此前曾支持15种语言，这是已经结束的历史实现。",
    "outside.md": "OTHER_PROJECT_PRIVATE：另一个项目默认支持全部语言。",
  };
  for (const [file, text] of Object.entries(sourceTexts)) await writeSourceFile(root, file, text);
  const state = Object.fromEntries(Object.entries(sourceTexts).map(([file, text]) =>
    [file, { hash: sha256Hex(text), concepts: [file === "outside.md" ? "outside" : "game-runtime"] }]));
  await writeSourceState(root, state);
  await writePage(path.join(root, "wiki/concepts"), "game-runtime",
    { title: "小游戏宿主与更新", summary: "历史验收概览", projectId: GAME_PROJECT, updatedAt: "2026-09-18" },
    ["## 适用范围", `${sourceTexts["scope.md"]} ^[scope.md:1]`,
      "## 存档保留", `${sourceTexts["save.md"]} ^[save.md:1]`,
      "## 当前语言范围", `${language} ^[language.md:1]`,
      "此前15种语言的实现属于历史资料；本决定仅适用于当前小游戏。",
      "## 历史语言实现", `${sourceTexts["history.md"]} ^[history.md:1]`].join("\n\n"));
  await writePage(path.join(root, "wiki/concepts"), "outside",
    { title: "小游戏语言支持", projectId: "other-project" }, `${sourceTexts["outside.md"]} ^[outside.md:1]`);
}
