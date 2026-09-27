/** Host-context policy tests use the same on-disk pages as the task reader. */
import { describe, expect, it } from "vitest";
import path from "node:path";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { writePage } from "./fixtures/write-page.js";
import {
  GAME_PAGE,
  GAME_PROJECT,
  LANGUAGE_DECISION,
  LANGUAGE_QUESTION,
  seedTaskWiki,
  useTaskWikiRoot,
} from "./fixtures/task-context-wiki.js";
import {
  readPersistedState,
  sha256Hex,
  writeSourceFile,
  writeSourceState,
} from "./fixtures/state-json.js";

const taskWiki = useTaskWikiRoot("hook-context-policy");

function input(prompt = LANGUAGE_QUESTION, seen: Record<string, string> = {}) {
  return {
    config: { wikiRoot: taskWiki.value, maxContextChars: 1500 },
    projectId: GAME_PROJECT,
    prompt,
    allowedPageIds: [GAME_PAGE],
    seen,
    // Kept deliberately to prove that the legacy operational payload is ignored.
    operationalContext: "OLD_RUNTIME: writer=old-machine",
  };
}

describe("knowledge flow context policy", () => {
  it("refreshes the primary decision when the same question is asked again", async () => {
    const first = await buildHookContext(input());
    const repeated = await buildHookContext(input(LANGUAGE_QUESTION, first.seen));

    // A prepared-context cache is not a delivery acknowledgement. The primary
    // decision stays available after a lost or compacted host turn.
    expect(repeated.context).toContain(LANGUAGE_DECISION);
    expect(repeated.seen).toEqual(first.seen);
  });

  it("does not inject operational README prose for a miss or a repeated hit", async () => {
    const miss = await buildHookContext(input("明天的气温和降雨是多少？"));
    expect(miss.context).not.toContain("OLD_RUNTIME");
    expect(miss.context).not.toContain("README");

    const first = await buildHookContext(input());
    const repeated = await buildHookContext(input(LANGUAGE_QUESTION, first.seen));
    expect(repeated.context).not.toContain("OLD_RUNTIME");
  });

  it("offers a verified project and current page pointer when no section matches", async () => {
    const result = await buildHookContext(input("UnicornQuaternionZyx"));
    expect(result.context).toContain(`项目：${GAME_PROJECT}`);
    expect(result.context).toContain(GAME_PAGE);
    expect(result.context).toContain("未找到可引用的有效决定");
    expect(result.context).not.toContain(LANGUAGE_DECISION);
  });

  it("excludes stale, archived, contradicted, unsourced and cross-project pages", async () => {
    await seedPolicyPages(taskWiki.value);
    const pageIds = [
      GAME_PAGE,
      "concepts/stale-only",
      "concepts/archived-only",
      "concepts/contradicted-only",
      "concepts/unsourced-only",
      "concepts/foreign-only",
    ];
    const result = await buildHookContext({ ...input(), allowedPageIds: pageIds });

    expect(result.context).toContain(LANGUAGE_DECISION);
    expect(result.context).not.toContain("STALE_ONLY");
    expect(result.context).not.toContain("ARCHIVED_ONLY");
    expect(result.context).not.toContain("CONTRADICTED_ONLY");
    expect(result.context).not.toContain("UNSOURCED_ONLY");
    expect(result.context).not.toContain("FOREIGN_ONLY");
  });

  it("reports degraded lexical retrieval while retaining sourced Chinese evidence", async () => {
    const result = await buildHookContext(input());

    // The fixture has no v3 embedding store, so the lexical path is expected to
    // remain useful and to expose the degraded status to the host.
    expect(result.context).toContain(LANGUAGE_DECISION);
    expect(result.status).toBe("degraded");
    expect(result.context).toMatch(/检索降级|embedding/);
  });

  it("does not cut a decision section or mark it seen when the host budget is too small", async () => {
    const result = await buildHookContext({
      ...input(),
      config: { wikiRoot: taskWiki.value, maxContextChars: 100 },
    });

    expect(result.context.length).toBeLessThanOrEqual(100);
    expect(result.context).not.toContain(LANGUAGE_DECISION.slice(0, 8));
    expect(result.seen).toEqual({});
    expect(result.complete).toBe(false);
  });

  it("enforces the native hook hard cap even when the configured budget is larger", async () => {
    await seedTaskWiki(taskWiki.value, `${LANGUAGE_DECISION} ${"补充决策证据".repeat(800)}`);
    const result = await buildHookContext({
      ...input(),
      config: { wikiRoot: taskWiki.value, maxContextChars: 20000 },
    });

    expect(result.context.length).toBeLessThanOrEqual(6000);
  });
});

/** Add policy pages whose only meaningful difference is freshness/scope. */
async function seedPolicyPages(projectRoot: string): Promise<void> {
  const pages = [
    {
      slug: "stale-only",
      marker: "STALE_ONLY",
      source: "stale-only.md",
      fields: { title: "过期语言决定", projectId: GAME_PROJECT },
      sourceText: "STALE_ONLY 旧语言决定。",
      recordedText: "STALE_ONLY 记录版本。",
    },
    {
      slug: "archived-only",
      marker: "ARCHIVED_ONLY",
      source: "archived-only.md",
      fields: { title: "归档语言决定", projectId: GAME_PROJECT, archived: true },
      sourceText: "ARCHIVED_ONLY 已退出的临时决定。",
      recordedText: "ARCHIVED_ONLY 已退出的临时决定。",
    },
    {
      slug: "contradicted-only",
      marker: "CONTRADICTED_ONLY",
      source: "contradicted-only.md",
      fields: { title: "冲突语言决定", projectId: GAME_PROJECT, contradictedBy: [GAME_PAGE] },
      sourceText: "CONTRADICTED_ONLY 被新决定推翻。",
      recordedText: "CONTRADICTED_ONLY 被新决定推翻。",
    },
    {
      slug: "unsourced-only",
      marker: "UNSOURCED_ONLY",
      source: "missing-only.md",
      fields: { title: "无来源语言决定", projectId: GAME_PROJECT },
      sourceText: "UNSOURCED_ONLY 没有可验证来源。",
      recordedText: null,
    },
    {
      slug: "foreign-only",
      marker: "FOREIGN_ONLY",
      source: "outside.md",
      fields: { title: "其他项目语言决定", projectId: "other-project" },
      sourceText: "FOREIGN_ONLY 属于另一个项目。",
      recordedText: null,
    },
  ] as const;

  for (const page of pages) {
    if (page.recordedText !== null) await writeSourceFile(projectRoot, page.source, page.sourceText);
    await writePage(
      path.join(projectRoot, "wiki/concepts"),
      page.slug,
      page.fields,
      `## 当前语言范围\n${page.marker} 当前小游戏语言决定。 ^[${page.source}:1]`,
    );
  }

  const prior = await readPersistedState(projectRoot);
  const sources = Object.fromEntries(Object.entries(prior.sources).map(([file, entry]) => [
    file,
    { hash: entry.hash, concepts: entry.concepts },
  ]));
  for (const page of pages) {
    if (page.recordedText === null) continue;
    sources[page.source] = {
      hash: sha256Hex(page.recordedText),
      concepts: [page.slug],
    };
  }
  await writeSourceState(projectRoot, sources);
}
