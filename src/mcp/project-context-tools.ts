/** Read-only business project discovery and decision evidence for agent task preparation. */
import { realpath } from "node:fs/promises";
import { z } from "zod";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { collectViewerPages } from "../viewer/collect.js";
import { buildTaskContext } from "../context/task.js";
import { readReviewedClaims, reviewedClaimRevision, type ReviewedClaim } from "../context/ledger.js";
import { sourceProjectIds } from "../utils/topic-scope.js";
import { jsonResult } from "./result.js";

interface ProjectInventoryItem { projectId: string; label: string; pages: number; claims: number }

/** Both automatic hooks and this tool consume buildTaskContext's scoped evidence contract. */
export function registerProjectContextTools(server: McpServer, root: string): void {
  registerInventoryTool(server, root);
  registerProjectContextTool(server, root);
  registerSemanticContextTool(server, root);
  registerClaimReadTool(server, root);
}

/** List project identifiers and counts without returning claim bodies. */
function registerInventoryTool(server: McpServer, root: string): void {
  server.registerTool("list_knowledge_projects", {
    title: "List Knowledge Projects",
    description: "Discover project IDs and page counts before querying project-specific decisions. " +
      "Read-only; no model call. Does not return other projects' knowledge bodies.",
    inputSchema: {},
  }, async () => jsonResult(await projectInventory(root)));
}

/** Register project-scoped page and claim retrieval. */
function registerProjectContextTool(server: McpServer, root: string): void {
  server.registerTool("get_project_context", {
    title: "Get Project Decision Context",
    description: "Before planning or answering a project decision, retrieve relevant accepted decision sections, " +
      "scope qualifications, page revisions, reviewed claims and matching sources. Ledger evidence has a claimRef " +
      "but no pageId; expand it with read_knowledge_claim. Use read_page only for returned page IDs. " +
      "Use the verified projectId from the hook or list_knowledge_projects; do not guess a platform or broaden scope. " +
      "On incomplete evidence, changed questions, conflicting historical decisions or precise state requirements, " +
      "expand claim references with read_knowledge_claim or actual page IDs with read_page before concluding. Read-only; no generation model.",
    inputSchema: {
      projectId: z.string().trim().min(1).max(200).describe("Verified project ID, not a page slug."),
      prompt: z.string().trim().min(1).max(12000).describe("The actual task question, including the decision object."),
    },
  }, async ({ projectId, prompt }) => jsonResult(await buildTaskContext({ root, projectId, prompt })));
}

/** Register cross-project semantic retrieval with explicit source-project caveats. */
function registerSemanticContextTool(server: McpServer, root: string): void {
  server.registerTool("get_knowledge_context", {
    title: "Get Semantic Knowledge Context",
    description: "Search accepted concept decisions by topic across projects. An optional projectId describes the " +
      "current task only; it does not restrict the search. Results include sourceProjectIds, qualifications, " +
      "page revisions, reviewed claims and matching sources. Expand any claimRef with read_knowledge_claim. " +
      "Check whether the source project's evidence applies before using it " +
      "for the current project. Read-only; no generation model.",
    inputSchema: {
      prompt: z.string().trim().min(1).max(12000).describe("The actual topic or decision question."),
      projectId: z.string().trim().min(1).max(200).optional().describe("Optional current-task project ID for context."),
    },
  }, async ({ projectId, prompt }) => jsonResult(await buildTaskContext({
    root, projectId, prompt, scope: "semantic",
  })));
}

/** Register exact claim expansion for pointers returned by context tools. */
function registerClaimReadTool(server: McpServer, root: string): void {
  server.registerTool("read_knowledge_claim", {
    title: "Read Reviewed Knowledge Claim",
    description: "Expand one claimRef returned by project or semantic context into its complete verified claim, " +
      "exact evidence quotes, content revision, generation ID and superseded status. Unknown references return " +
      "no-hit; an invalid or changed projection returns degraded without claim content. Read-only; no generation model.",
    inputSchema: {
      claimRef: z.string().trim().regex(/^[0-9a-f]{64}:(?:0|[1-9]\d*)$/)
        .describe("Exact 64-hex recordId:claimIndex reference returned by a context tool."),
    },
  }, async ({ claimRef }) => jsonResult(await readKnowledgeClaim(root, claimRef)));
}

/** Legacy owners and semantic source IDs share one project discovery inventory. */
async function projectInventory(root: string): Promise<{ projects: ProjectInventoryItem[]; status?: string; warning?: string }> {
  const pinnedRoot = await realpath(root);
  const [pages, ledger] = await Promise.all([collectViewerPages(pinnedRoot), readReviewedClaims(pinnedRoot)]);
  const labels = projectLabels(pages);
  for (const claim of ledger.projection?.claims ?? []) {
    if (!labels.has(claim.projectId)) labels.set(claim.projectId, claim.projectLabel || claim.projectId);
  }
  const projects = new Map<string, ProjectInventoryItem>();
  addPageProjects(projects, pages, labels);
  addLedgerProjects(projects, ledger.projection?.claims ?? [], labels);
  return { projects: [...projects.values()].sort((a, b) => a.projectId.localeCompare(b.projectId)),
    ...(ledger.warning ? { status: "degraded", warning: ledger.warning } : {}) };
}

/** Count each visible page once for each of its declared source projects. */
function addPageProjects(projects: Map<string, ProjectInventoryItem>,
  pages: Awaited<ReturnType<typeof collectViewerPages>>, labels: Map<string, string>): void {
  for (const page of pages) {
    if (page.frontmatter.archived || page.frontmatter.orphaned) continue;
    for (const projectId of sourceProjectIds(page.frontmatter)) {
      const project = projects.get(projectId) ?? { projectId, label: labels.get(projectId) ?? projectId, pages: 0, claims: 0 };
      project.pages += 1;
      projects.set(projectId, project);
    }
  }
}

/** Add ledger-only project identities and count claim refs without exposing their text. */
function addLedgerProjects(projects: Map<string, ProjectInventoryItem>, claims: ReviewedClaim[], labels: Map<string, string>): void {
  for (const claim of claims) {
    const project = projects.get(claim.projectId) ?? {
      projectId: claim.projectId, label: labels.get(claim.projectId) ?? claim.projectLabel ?? claim.projectId,
      pages: 0, claims: 0,
    };
    project.claims += 1;
    projects.set(project.projectId, project);
  }
}

/** Return one sealed claim by exact reference; never recover from live or unverified files. */
async function readKnowledgeClaim(root: string, claimRef: string): Promise<Record<string, unknown>> {
  const pinnedRoot = await realpath(root);
  const result = await readReviewedClaims(pinnedRoot);
  if (result.warning) return { status: "degraded", claimRef, warning: result.warning };
  const projection = result.projection;
  if (!projection) return { status: "no-hit", claimRef };
  const claim = [...projection.claims, ...projection.superseded].find(item => item.claimRef === claimRef);
  if (!claim) return { status: "no-hit", claimRef, generationId: projection.generationId };
  return { status: "ok", claim, revision: reviewedClaimRevision(claim),
    generationId: projection.generationId, superseded: claim.superseded };
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
