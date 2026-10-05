/** Given/When/Then coverage for cross-project semantic evidence and topic navigation. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { buildTaskContext } from "../src/context/task.js";
import { sourceProjectIds } from "../src/utils/topic-scope.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { generateProjectNavigation } from "../extensions/knowledge-flow/topic-navigation.js";
import { buildEmbeddingText } from "../src/utils/embeddings-pages.js";
import { hashChunkText, splitIntoChunks } from "../src/utils/retrieval.js";
import { CONCEPTS_DIR } from "../src/utils/constants.js";
import { useAimockLifecycle, mockOpenAIEnv } from "./fixtures/aimock-helper.js";
import { writePage } from "./fixtures/write-page.js";
import { sha256Hex, writeSourceFile, writeSourceState } from "./fixtures/state-json.js";
import { fingerprintForEnv, writeV3EmbeddingStore } from "./fixtures/v3-embedding-store.js";
import { buildServer, callTool, useMcpRoot } from "./fixtures/mcp-test-env.js";
import { topicFixture } from "./knowledge-flow-topic-fixtures.js";

const aimock = useAimockLifecycle("semantic-context");
const mcpRoot = useMcpRoot("semantic-context-mcp");
const MATCH = [1, 0, 0, 0, 0, 0, 0, 0];
const OTHER = [0, 1, 0, 0, 0, 0, 0, 0];
const QUESTION = "怎样避免线上升级让玩家资料丢失？";

afterEach(() => vi.unstubAllEnvs());

interface PageSeed {
  slug: string;
  title: string;
  fields: Record<string, unknown>;
  text: string;
  source: string;
  recordedHash?: string;
  cited?: boolean;
  heading?: string;
}

/** Section body shared by the page writer and the embedding chunks so chunks overlap the written section. */
function sectionBody(page: Pick<PageSeed, "heading" | "text">, citation: string): string {
  return `## ${page.heading ?? "Current decision"}\n${page.text}${citation}`;
}

/** Prepare the deterministic embedding endpoint shared by semantic acceptance cases. */
async function semanticRoot(): Promise<string> {
  const handle = await aimock.start();
  handle.mock.onEmbedding(/.*/, { embedding: MATCH });
  for (const [key, value] of Object.entries(mockOpenAIEnv(handle))) vi.stubEnv(key, value ?? "");
  vi.stubEnv("LLMWIKI_EMBEDDING_PROVIDER", "openai");
  return aimock.makeWorkspace("# semantic context fixture\n");
}

/** Write pages, their real cited sources, and source ownership state to a temporary wiki. */
async function seedPages(root: string, pages: PageSeed[]): Promise<void> {
  const sourceState: Record<string, { hash: string; concepts: string[] }> = {};
  for (const page of pages) {
    await mkdir(path.join(root, CONCEPTS_DIR), { recursive: true });
    const sourceFile = `${page.slug}.md`;
    const sourceText = `${page.source}\n`;
    await writeSourceFile(root, sourceFile, sourceText);
    sourceState[sourceFile] = { hash: page.recordedHash ?? sha256Hex(sourceText), concepts: [page.slug] };
    const citation = page.cited === false ? "" : ` ^[${sourceFile}:1]`;
    await writePage(path.join(root, CONCEPTS_DIR), page.slug, page.fields, sectionBody(page, citation));
  }
  await writeSourceState(root, sourceState);
}

/** Build a real v3 chunk store whose target page is the sole semantic match. */
async function seedEmbeddings(root: string, pages: PageSeed[], targetSlug: string): Promise<void> {
  const entries = pages.map(page => ({ id: `concepts/${page.slug}`, title: page.title,
    summary: String(page.fields.summary ?? ""), body: sectionBody(page, ` ^[${page.slug}.md:1]`) }));
  const chunks = entries.flatMap(page => splitIntoChunks(page.body).map((text, chunkIndex) => ({
    pageId: page.id, title: page.title, chunkIndex, contentHash: hashChunkText(text), text,
    vector: page.id === `concepts/${targetSlug}` ? MATCH : OTHER,
  })));
  await writeV3EmbeddingStore(root, { model: "text-embedding-3-small", vector: MATCH,
    fingerprint: fingerprintForEnv(process.env),
    entries: entries.map(page => ({ pageId: page.id, title: page.title, summary: page.summary,
      embeddingTextHash: hashChunkText(buildEmbeddingText({ title: page.title, summary: page.summary })), vector: MATCH })),
    chunks });
}

function semanticPage(slug: string, sourceProjectIds: string[], text: string): PageSeed {
  return { slug, title: "玩家资料连续性", fields: { title: "玩家资料连续性", topicScope: "semantic",
    sourceProjectIds, knowledgeTopic: "玩家资料连续性", summary: "保留玩家身份和存档" },
    text, source: text.replace(/\s+/g, " ") };
}

describe("semantic knowledge context", () => {
  it("Given a synonym query from A, When searching semantic scope, Then returns B's cited decision and source", async () => {
    const root = await semanticRoot();
    const fromA: PageSeed = { slug: "project-a", title: "A note", fields: { title: "A note", projectId: "project-a" },
      text: "A unrelated release note remains isolated.", source: "A unrelated release note remains isolated." };
    const fromB = semanticPage("project-b", ["project-b"],
      "CANONICAL_DECISION: 升级时保留玩家资料。retain the original player identity and save data during updates.");
    const pages = [fromA, fromB];
    await seedPages(root, pages);
    await seedEmbeddings(root, pages, fromB.slug);

    const result = await buildTaskContext({ root, projectId: "project-a", scope: "semantic", prompt: QUESTION });

    expect(result.evidence.map(item => item.pageId)).toEqual(["concepts/project-b"]);
    expect(result.evidence[0].sourceProjectIds).toEqual(["project-b"]);
    expect(result.evidence[0].text).toContain("CANONICAL_DECISION");
    expect(result.evidence[0].sources).toEqual([expect.objectContaining({ file: "project-b.md", start: 1 })]);

    const hook = await buildHookContext({ config: { wikiRoot: root, topicScope: "semantic" }, projectId: "project-a",
      prompt: QUESTION, allowedPageIds: pages.map(page => `concepts/${page.slug}`), seen: {} });
    expect(hook.context).toContain("来源项目与适用范围可能不同，不能直接套用");
    expect(hook.context).toContain("get_knowledge_context");
    expect(hook.context).toContain("来源项目：project-b");
  });

  it("Given an explicit allowed set, When semantic retrieval runs, Then it cannot return outside pages", async () => {
    const root = await semanticRoot();
    const fromA: PageSeed = { slug: "project-a", title: "A note", fields: { projectId: "project-a" },
      text: "A unrelated release note remains isolated.", source: "A unrelated release note remains isolated." };
    const fromB = semanticPage("project-b", ["project-b"], "CANONICAL_DECISION: retain player data during updates.");
    const pages = [fromA, fromB];
    await seedPages(root, pages);
    await seedEmbeddings(root, pages, fromB.slug);

    const result = await buildTaskContext({ root, projectId: "project-a", scope: "semantic", prompt: QUESTION,
      allowedPageIds: ["concepts/project-a"] });

    expect(result.evidence.map(item => item.pageId)).toEqual([]);
    expect(JSON.stringify(result)).not.toContain("CANONICAL_DECISION");
  });

  it("Given legacy project mode, When another project's page matches, Then foreign evidence stays excluded", async () => {
    const root = await semanticRoot();
    const pages: PageSeed[] = [
      { slug: "project-a", title: "A decision", fields: { projectId: "project-a" },
        text: "保留玩家账号连续性并保存升级后的进度。", source: "保留玩家账号连续性并保存升级后的进度。" },
      { slug: "project-b", title: "B decision", fields: { projectId: "project-b" },
        text: "FOREIGN_DECISION: 保留玩家账号连续性并保存升级后的进度。", source: "FOREIGN_DECISION: 保留玩家账号连续性并保存升级后的进度。" },
    ];
    await seedPages(root, pages);
    await seedEmbeddings(root, pages, "project-b");

    const result = await buildTaskContext({ root, projectId: "project-a", prompt: "保留玩家账号连续性并保存升级后的进度" });

    expect(result.evidence.map(item => item.pageId)).toEqual(["concepts/project-a"]);
    expect(JSON.stringify(result)).not.toContain("FOREIGN_DECISION");
  });

  it("Given a shared semantic page, When A and B use project scope, Then both can discover it with provenance", async () => {
    const root = await semanticRoot();
    const shared = semanticPage("shared-continuity", ["project-a", "project-b"],
      "SHARED_DECISION: preserve the original player identity and save data during updates.");
    await seedPages(root, [shared]);

    const fromA = await buildTaskContext({ root, projectId: "project-a", prompt: "preserve player identity" });
    const fromB = await buildTaskContext({ root, projectId: "project-b", prompt: "preserve player identity" });

    expect(fromA.evidence[0].sourceProjectIds).toEqual(["project-a", "project-b"]);
    expect(fromB.evidence[0].pageId).toBe("concepts/shared-continuity");
  });

  it("Given a semantic MCP request, When calling the registered tool, Then it searches without project ownership", async () => {
    const root = mcpRoot.value;
    await seedPages(root, [semanticPage("shared-continuity", ["project-b"],
      "MCP_SHARED: preserve the original player identity and save data during updates.")]);
    const response = await callTool(buildServer(root), "get_knowledge_context", { prompt: "preserve player identity", projectId: "project-a" });
    const result = response.structuredContent?.result as { projectId: string | null; evidence: Array<{ sourceProjectIds?: string[] }> };

    expect(result.projectId).toBe("project-a");
    expect(result.evidence[0].sourceProjectIds).toEqual(["project-b"]);
  });

  it("Given invalidated, archived, contradicted, or uncited pages, When queried, Then none is injected", async () => {
    const root = await semanticRoot();
    const stale = semanticPage("stale", ["project-b"], "STALE_DECISION: retain player data after upgrade.");
    stale.recordedHash = sha256Hex("old source\n");
    const archived = semanticPage("archived", ["project-b"], "ARCHIVED_DECISION: retain player data after upgrade.");
    archived.fields.archived = true;
    const contradicted = semanticPage("contradicted", ["project-b"], "CONTRADICTED_DECISION: retain player data after upgrade.");
    contradicted.fields.contradictedBy = ["concepts/replacement"];
    const uncited = semanticPage("uncited", ["project-b"], "UNSOURCED_DECISION: retain player data after upgrade.");
    uncited.text = uncited.text.replace("UNSOURCED_DECISION: ", "");
    uncited.source = uncited.text;
    uncited.cited = false;
    const usable = semanticPage("usable", ["project-b"], "USABLE_DECISION: retain player data after upgrade.");
    await seedPages(root, [stale, archived, contradicted, uncited, usable]);

    const result = await buildTaskContext({ root, scope: "semantic", prompt: "retain player data after upgrade" });

    expect(result.evidence.map(item => item.pageId)).toEqual(["concepts/usable"]);
    expect(JSON.stringify(result)).not.toMatch(/STALE_DECISION|ARCHIVED_DECISION|CONTRADICTED_DECISION|UNSOURCED_DECISION/);
  });
});

describe("cross-project semantic gate", () => {
  const THIN_PROMPT = "玩家存档";
  const thinPage = (sourceProjects: string[]) => ({ ...semanticPage("thin-decision", sourceProjects,
    "THIN_DECISION: retain data during updates."), heading: "玩家" });

  async function runThin(sourceProjects: string[], prompt = THIN_PROMPT): Promise<string[]> {
    const root = await semanticRoot();
    const page = thinPage(sourceProjects);
    await seedPages(root, [page]);
    await seedEmbeddings(root, [page], page.slug);
    const result = await buildTaskContext({ root, projectId: "project-a", scope: "semantic", prompt });
    return result.evidence.map(item => item.pageId);
  }

  it("Given another project's page at semantic 1.0 matching one query word, When searching, Then it is not injected", async () => {
    expect(await runThin(["project-b"])).toEqual([]);
  });

  it("Given the same page and match owned by the current project, When searching, Then it is injected", async () => {
    expect(await runThin(["project-a"])).toEqual(["concepts/thin-decision"]);
  });

  it("Given a single surviving query word, When searching, Then topical retrieval is skipped", async () => {
    expect(await runThin(["project-a"], "玩家？")).toEqual([]);
  });
});

describe("cross-project domain-term gate", () => {
  const WORKFLOW_PAGE_TEXT = "合并前先清理分支，部署后再做收尾验证。";
  const DOMAIN_PAGE_TEXT = "玩家存档与账号数据在更新中保留。";

  async function runPage(text: string, sourceProjects: string[], prompt: string): Promise<string[]> {
    const root = await semanticRoot();
    const page = { ...semanticPage("domain-gate", sourceProjects, text), heading: "流程" };
    await seedPages(root, [page]);
    await seedEmbeddings(root, [page], page.slug);
    const result = await buildTaskContext({ root, projectId: "project-a", scope: "semantic", prompt });
    return result.evidence.map(item => item.pageId);
  }

  it("Given another project's page matching only workflow words, When asking to merge and deploy, Then it is not injected", async () => {
    expect(await runPage(WORKFLOW_PAGE_TEXT, ["project-b"], "合并之后部署，然后清理收尾")).toEqual([]);
  });

  it("Given the same workflow-only match owned by the current project, When asking, Then it is injected", async () => {
    expect(await runPage(WORKFLOW_PAGE_TEXT, ["project-a"], "合并之后部署，然后清理收尾")).toEqual(["concepts/domain-gate"]);
  });

  it("Given another project's page matching two domain words, When asking, Then it is injected", async () => {
    expect(await runPage(DOMAIN_PAGE_TEXT, ["project-b"], "玩家存档和账号数据怎么处理")).toEqual(["concepts/domain-gate"]);
  });

  it("Given another project's page matching one domain word and workflow words, When asking, Then it is not injected", async () => {
    expect(await runPage("ROAS 在合并部署时记录。", ["project-b"], "合并部署时 ROAS 怎么办")).toEqual([]);
  });
});

describe("semantic MCP and topic navigation", () => {
  it("Given mixed frontmatter, When canonicalizing sources, Then deduplicates sorted IDs and keeps legacy ownership", () => {
    expect(sourceProjectIds({ sourceProjectIds: ["project-b", "", "project-a", "project-b"], projectId: "project-c" }))
      .toEqual(["project-a", "project-b", "project-c"]);
  });

  it("Given source-only semantic pages, When listing projects, Then counts each source project", async () => {
    const root = mcpRoot.value;
    await seedPages(root, [semanticPage("shared-continuity", ["project-a", "project-b"], "A shared decision. cite.")]);
    const response = await callTool(buildServer(root), "list_knowledge_projects", {});
    const result = response.structuredContent?.result as { projects: Array<{ projectId: string; pages: number }> };
    expect(result.projects).toEqual(expect.arrayContaining([
      expect.objectContaining({ projectId: "project-a", pages: 1 }),
      expect.objectContaining({ projectId: "project-b", pages: 1 }),
    ]));
  });

  it("Given semantic topic metadata, When navigation is generated, Then topics group pages and show source projects", async () => {
    const base = await topicFixture();
    const config = { ...base, topicScope: "semantic" as const,
      projects: { "project-a": { label: "Alpha" }, "project-b": { label: "Beta" } } };
    await writePage(path.join(config.wikiRoot, "wiki/concepts"), "shared-continuity",
      { title: "玩家资料连续性", topicScope: "semantic", sourceProjectIds: ["project-a", "project-b"],
        knowledgeTopic: "玩家资料连续性", summary: "更新中保留账号和存档" }, "Shared decision.");

    await generateProjectNavigation(config);

    const moc = await readFile(path.join(config.wikiRoot, "wiki/MOC.md"), "utf8");
    expect(moc).toContain("## 玩家资料连续性");
    expect(moc).toContain("来源项目：Alpha、Beta");
    expect(moc).toContain("[[concepts/shared-continuity|玩家资料连续性]]");
  });
});
