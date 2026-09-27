/** Read-only business project discovery and decision evidence for agent task preparation. */
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { collectViewerPages } from "../viewer/collect.js";
import { buildTaskContext } from "../context/task.js";
import { sourceProjectIds } from "../utils/topic-scope.js";
import { jsonResult } from "./result.js";

/** Both automatic hooks and this tool consume buildTaskContext's scoped evidence contract. */
export function registerProjectContextTools(server: McpServer, root: string): void {
  server.registerTool("list_knowledge_projects", {
    title: "List Knowledge Projects",
    description: "Discover project IDs and page counts before querying project-specific decisions. " +
      "Read-only; no model call. Does not return other projects' knowledge bodies.",
    inputSchema: {},
  }, async () => jsonResult({ projects: await projectInventory(root) }));
  server.registerTool("get_project_context", {
    title: "Get Project Decision Context",
    description: "Before planning or answering a project decision, retrieve relevant accepted decision sections, " +
      "scope qualifications, page revisions and matching sources. Use the verified projectId from the hook " +
      "or list_knowledge_projects; do not guess a platform or broaden an ambiguous scope. " +
      "On incomplete evidence, changed questions, conflicting historical decisions or precise state requirements, " +
      "read_page for the returned page ID's slug before concluding. Read-only; no generation model.",
    inputSchema: {
      projectId: z.string().trim().min(1).max(200).describe("Verified project ID, not a page slug."),
      prompt: z.string().trim().min(1).max(12000).describe("The actual task question, including the decision object."),
    },
  }, async ({ projectId, prompt }) => jsonResult(await buildTaskContext({ root, projectId, prompt })));
  server.registerTool("get_knowledge_context", {
    title: "Get Semantic Knowledge Context",
    description: "Search accepted concept decisions by topic across projects. An optional projectId describes the " +
      "current task only; it does not restrict the search. Results include sourceProjectIds, qualifications, " +
      "page revisions and matching sources. Check whether the source project's evidence applies before using it " +
      "for the current project. Read-only; no generation model.",
    inputSchema: {
      prompt: z.string().trim().min(1).max(12000).describe("The actual topic or decision question."),
      projectId: z.string().trim().min(1).max(200).optional().describe("Optional current-task project ID for context."),
    },
  }, async ({ projectId, prompt }) => jsonResult(await buildTaskContext({
    root, projectId, prompt, scope: "semantic",
  })));
}

/** Legacy owners and semantic source IDs share one project discovery inventory. */
async function projectInventory(root: string): Promise<{ projectId: string; label: string; pages: number }[]> {
  const pages = await collectViewerPages(root);
  const labels = projectLabels(pages);
  const projects = new Map<string, { projectId: string; label: string; pages: number }>();
  for (const page of pages) {
    if (page.frontmatter.archived || page.frontmatter.orphaned) continue;
    for (const projectId of sourceProjectIds(page.frontmatter)) {
      const project = projects.get(projectId) ?? { projectId, label: labels.get(projectId) ?? projectId, pages: 0 };
      project.pages += 1;
      projects.set(projectId, project);
    }
  }
  return [...projects.values()].sort((a, b) => a.projectId.localeCompare(b.projectId));
}

/** Resolve display labels from legacy project pages before counting semantic references. */
function projectLabels(pages: Awaited<ReturnType<typeof collectViewerPages>>): Map<string, string> {
  const labels = new Map<string, string>();
  for (const page of pages) {
    const projectId = page.frontmatter.projectId;
    const label = page.frontmatter.projectLabel;
    if (typeof projectId === "string" && typeof label === "string" && label.trim()) labels.set(projectId, label);
  }
  return labels;
}
