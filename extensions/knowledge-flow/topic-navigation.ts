/** Render the shared wiki's primary navigation by project and coherent topic rather than technical filenames. */
import path from "node:path";
import { readdir, readFile } from "node:fs/promises";
import { atomicWrite, parseFrontmatter } from "../../src/utils/markdown.js";
import { confineUnderRoot } from "../../src/utils/path-confine.js";
import { sourceProjectIds } from "../../src/utils/topic-scope.js";
import type { FlowConfig } from "./types.js";

interface NavigationPage { pageId: string; title: string; project: string; topic: string; sourceProjects: string[]; summary: string; }

/** Build a deterministic project entry point using explicit ownership metadata. */
export async function generateProjectNavigation(config: FlowConfig): Promise<void> {
  const folder = path.join(config.wikiRoot, "wiki/concepts");
  const names = (await readdir(folder)).filter(name => name.endsWith(".md")).sort();
  const pages = await Promise.all(names.map(name => navigationPage(config, name)));
  if (config.topicScope === "semantic") return generateSemanticNavigation(config, pages.filter(isNavigationPage));
  const groups = new Map<string, NavigationPage[]>();
  for (const page of pages.filter(isNavigationPage)) groups.set(page.project, [...(groups.get(page.project) ?? []), page]);
  const lines = ["# 项目知识导航", "", "按项目阅读主题页；原始证据保留在 sources 中。", ""];
  for (const label of [...groups.keys()].sort()) {
    lines.push(`## ${label}`, "");
    for (const page of groups.get(label)!) {
      lines.push(`- [[${page.pageId}|${linkLabel(page.title)}]]${page.summary ? ` — ${page.summary}` : ""}`);
    }
    lines.push("");
  }
  await atomicWrite(path.join(config.wikiRoot, "wiki/MOC.md"), lines.join("\n"), { confineRoot: config.wikiRoot });
}

async function navigationPage(config: FlowConfig, name: string): Promise<NavigationPage | null> {
  const pageId = `concepts/${name.slice(0, -3)}`;
  const file = await confineUnderRoot(path.join("wiki", `${pageId}.md`), config.wikiRoot, { mustExist: true });
  const { meta } = parseFrontmatter(await readFile(file, "utf8"));
  if (meta.orphaned) return null;
  const title = typeof meta.title === "string" ? meta.title : name.slice(0, -3);
  const ids = sourceProjectIds(meta);
  return { pageId, title, project: projectLabel(config, pageId, meta),
    topic: typeof meta.knowledgeTopic === "string" && meta.knowledgeTopic.trim() ? meta.knowledgeTopic.trim() : title,
    sourceProjects: ids.map(id => sourceProjectLabel(config, id, meta)),
    summary: typeof meta.summary === "string" ? meta.summary.replace(/\s+/g, " ").slice(0, 160) : "" };
}

/** Write topic groups with source projects visible beside each linked page. */
function generateSemanticNavigation(config: FlowConfig, pages: NavigationPage[]): Promise<void> {
  const groups = new Map<string, NavigationPage[]>();
  for (const page of pages) groups.set(page.topic, [...(groups.get(page.topic) ?? []), page]);
  const lines = ["# 主题知识导航", "", "按知识主题阅读；来源项目用于判断证据来源，适用范围以页面内容为准。", ""];
  for (const topic of [...groups.keys()].sort()) {
    lines.push(`## ${topic}`, "");
    for (const page of groups.get(topic)!) {
      const sources = page.sourceProjects.length ? `来源项目：${page.sourceProjects.join("、")}` : "来源项目：未标注";
      lines.push(`- [[${page.pageId}|${linkLabel(page.title)}]] — ${sources}${page.summary ? `；${page.summary}` : ""}`);
    }
    lines.push("");
  }
  return atomicWrite(path.join(config.wikiRoot, "wiki/MOC.md"), lines.join("\n"), { confineRoot: config.wikiRoot });
}

/** Prefer the legacy page label for its own project and config labels for semantic sources. */
function sourceProjectLabel(config: FlowConfig, projectId: string, meta: Record<string, unknown>): string {
  if (meta.projectId === projectId && typeof meta.projectLabel === "string") return meta.projectLabel;
  return config.projects?.[projectId]?.label || projectId;
}

/** Narrow parallel page reads after omitted/orphaned pages have been filtered. */
function isNavigationPage(page: NavigationPage | null): page is NavigationPage { return page !== null; }

function projectLabel(config: FlowConfig, pageId: string, meta: Record<string, unknown>): string {
  const owners = Object.entries(config.projects ?? {}).filter(([, project]) => project.pages?.includes(pageId));
  const owner = typeof meta.projectId === "string" ? meta.projectId : owners.length === 1 ? owners[0][0] : "";
  const label = config.projects?.[owner]?.label;
  return typeof meta.projectLabel === "string" ? meta.projectLabel : label || owner || "知识库使用";
}

function linkLabel(value: string): string { return value.replace(/[\[\]|\n\r]/g, " "); }
