/** Real v3 semantic retrieval and scoped hook-context acceptance tests. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { buildTaskContext } from "../src/context/task.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { CONCEPTS_DIR } from "../src/utils/constants.js";
import { buildEmbeddingText } from "../src/utils/embeddings-pages.js";
import { hashChunkText, splitIntoChunks } from "../src/utils/retrieval.js";
import { useAimockLifecycle, mockOpenAIEnv } from "./fixtures/aimock-helper.js";
import { writePage } from "./fixtures/write-page.js";
import { sha256Hex, writeSourceFile, writeSourceState } from "./fixtures/state-json.js";
import { fingerprintForEnv, writeV3EmbeddingStore } from "./fixtures/v3-embedding-store.js";

const aimock = useAimockLifecycle("task-context-semantic");
const VECTOR = [1, 0, 0, 0, 0, 0, 0, 0];
const OTHER_VECTOR = [0, 1, 0, 0, 0, 0, 0, 0];

afterEach(() => vi.unstubAllEnvs());

function applyEnv(env: NodeJS.ProcessEnv): void {
  for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value ?? "");
  vi.stubEnv("LLMWIKI_EMBEDDING_PROVIDER", "openai");
}

async function startMockedSemanticWorkspace(): Promise<string> {
  const handle = await aimock.start();
  handle.mock.onEmbedding(/.*/, { embedding: VECTOR });
  applyEnv(mockOpenAIEnv(handle));
  return aimock.makeWorkspace("# placeholder\n");
}

function sourceBody(decision: string, source: string): string {
  const filler = "The current policy preserves the original identity and keeps the release boundary explicit. ".repeat(10);
  return `${decision} ${filler} ^[${source}:1]`;
}

async function seedPage(root: string, slug: string, projectId: string, title: string, body: string): Promise<void> {
  await mkdir(path.join(root, CONCEPTS_DIR), { recursive: true });
  await writePage(path.join(root, CONCEPTS_DIR), slug,
    { title, projectId, summary: "Decision overview", updatedAt: "2026-09-18" }, body);
}

async function seedStore(root: string, pages: Array<{ id: string; title: string; summary: string; body: string }>, target: string): Promise<void> {
  const chunks = pages.flatMap((page) => splitIntoChunks(page.body).map((text, chunkIndex) => ({
    pageId: page.id, title: page.title, chunkIndex, contentHash: hashChunkText(text), text,
    vector: text.includes(target) ? VECTOR : OTHER_VECTOR, updatedAt: "2026-09-18T00:00:00.000Z",
  })));
  await writeV3EmbeddingStore(root, {
    model: "text-embedding-3-small",
    vector: VECTOR,
    fingerprint: fingerprintForEnv(process.env),
    entries: pages.map((page) => ({ pageId: page.id, title: page.title, summary: page.summary,
      embeddingTextHash: hashChunkText(buildEmbeddingText({ title: page.title, summary: page.summary })), vector: VECTOR })),
    chunks,
  });
}

async function seedSemanticWiki(root: string, projectId = "project-a"): Promise<{ pageId: string; body: string }> {
  const pageId = "concepts/game-a";
  const history = sourceBody("HISTORICAL_POLICY: the former release used a different host boundary.", "history.md");
  const decision = sourceBody("CURRENT_POLICY: the current release preserves the original game identity and player saves.", "current.md");
  const body = `## Historical notes\n${history}\n\n## Current decision\n${decision}`;
  await seedPage(root, "game-a", projectId, "Game Runtime", body);
  await writeSourceFile(root, "history.md", history.replace(" ^[history.md:1]", ""));
  await writeSourceFile(root, "current.md", decision.replace(" ^[current.md:1]", ""));
  await writeSourceState(root, {
    "history.md": { hash: sha256Hex(history.replace(" ^[history.md:1]", "")), concepts: ["game-a"] },
    "current.md": { hash: sha256Hex(decision.replace(" ^[current.md:1]", "")), concepts: ["game-a"] },
  });
  return { pageId, body };
}

describe("task context semantic retrieval", () => {
  it("finds a later current decision with no lexical overlap and its matching source", async () => {
    const root = await startMockedSemanticWorkspace();
    const { pageId, body } = await seedSemanticWiki(root);
    await seedStore(root, [{ id: pageId, title: "Game Runtime", summary: "Decision overview", body }], "CURRENT_POLICY");

    const task = await buildTaskContext({ root, projectId: "project-a", prompt: "如何处理上线后的玩家资料？", allowedPageIds: [pageId] });
    const hook = await buildHookContext({ config: { wikiRoot: root, maxContextChars: 5000 }, projectId: "project-a",
      prompt: "如何处理上线后的玩家资料？", allowedPageIds: [pageId], seen: {} });

    expect(task.status).toBe("ok");
    expect(task.evidence).toHaveLength(1);
    expect(task.evidence[0].text).toContain("CURRENT_POLICY");
    expect(task.evidence[0].sources[0].file).toBe("current.md");
    expect(task.evidence[0].sources[0].start).toBe(1);
    expect(task.evidence[0].sources.some((source) => source.file === "history.md")).toBe(false);
    expect(hook.context).toContain("CURRENT_POLICY");
    expect(hook.context).toContain("current.md:1");
    expect(hook.context).not.toContain("HISTORICAL_POLICY");
  });

  it("keeps semantic hits inside the declared project scope", async () => {
    const root = await startMockedSemanticWorkspace();
    const first = await seedSemanticWiki(root, "project-a");
    const foreignBody = sourceBody("FOREIGN_CURRENT: another project has the same semantic topic.", "foreign.md");
    await seedPage(root, "game-b", "project-b", "Other Runtime", `## Current decision\n${foreignBody}`);
    await writeSourceFile(root, "foreign.md", foreignBody.replace(" ^[foreign.md:1]", ""));
    await seedStore(root, [
      { id: first.pageId, title: "Game Runtime", summary: "Decision overview", body: first.body },
      { id: "concepts/game-b", title: "Other Runtime", summary: "Decision overview", body: `## Current decision\n${foreignBody}` },
    ], "CURRENT_POLICY");

    const result = await buildTaskContext({ root, projectId: "project-a", prompt: "如何处理上线后的玩家资料？",
      allowedPageIds: [first.pageId] });

    expect(result.evidence.map((item) => item.pageId)).toEqual([first.pageId]);
    expect(JSON.stringify(result)).not.toContain("FOREIGN_CURRENT");
  });

  it("degrades on embedding outage while lexical Chinese evidence remains sourced", async () => {
    const env = {
      LLMWIKI_PROVIDER: "openai", OPENAI_API_KEY: "test-key", LLMWIKI_MODEL: "gpt-4o",
      LLMWIKI_EMBEDDING_MODEL: "text-embedding-3-small", OPENAI_BASE_URL: "http://127.0.0.1:1/v1",
    };
    applyEnv(env);
    const root = await aimock.makeWorkspace("# placeholder\n");
    const pageId = "concepts/game-a";
    const decision = "当前决定：上线后保留原游戏身份，并保留玩家存档。";
    const history = "历史方案曾经清空玩家数据。";
    const body = `## 历史方案\n${history} ^[history.md:1]\n\n## 当前决定\n${decision} ^[current.md:1]`;
    await seedPage(root, "game-a", "project-a", "小游戏运行", body);
    await writeSourceFile(root, "history.md", history);
    await writeSourceFile(root, "current.md", decision);
    await writeSourceState(root, { "history.md": { hash: sha256Hex(history), concepts: ["game-a"] },
      "current.md": { hash: sha256Hex(decision), concepts: ["game-a"] } });
    await seedStore(root, [{ id: pageId, title: "小游戏运行", summary: "决策", body }], decision);

    const result = await buildHookContext({ config: { wikiRoot: root, maxContextChars: 5000 }, projectId: "project-a",
      prompt: "上线后怎样保留玩家存档？", allowedPageIds: [pageId], seen: {} });

    expect(result.status).toBe("degraded");
    expect(result.context).toContain(decision);
    expect(result.context).toContain("current.md:1");
    expect(result.context).toContain("检索降级");
    expect(result.context).not.toContain("历史方案曾经清空");
  });
});
