/** Read-only task evidence: pin a replica, verify project scope, then select sourced sections. */
import { realpath } from "node:fs/promises";
import { buildViewerSnapshot } from "../viewer/snapshot.js";
import type { ViewerPage } from "../viewer/types.js";
import { extractClaimCitations } from "../utils/markdown.js";
import { sourceProjectIds } from "../utils/topic-scope.js";
import { retrieveSemanticChunks } from "./retrieval.js";
import { createSourceWindowBudget, flattenCitations, materializeSourceWindows } from "./provenance.js";
import { rankDecisionSections, taskPageRevision, type DecisionSection } from "./task-sections.js";
import type { TaskContext, TaskContextOptions, TaskEvidence } from "./task-types.js";

const MAX_TASK_PAGES = 3;
const MAX_TASK_SECTIONS = 6;
const MAX_NO_HIT_PAGE_POINTERS = 12;
const MAX_EVIDENCE_CHARS = 16000;

/** Missing scope never silently becomes a whole-wiki read. */
export async function buildTaskContext(options: TaskContextOptions): Promise<TaskContext> {
  const result = emptyContext(options.projectId);
  if (options.scope !== "semantic" && !options.projectId && options.allowedPageIds === undefined) {
    return { ...result, status: "ambiguous-scope" };
  }
  const root = await realpath(options.root);
  const snapshot = await buildViewerSnapshot(root);
  const scoped = snapshot.pages.filter(page => inScope(page, options));
  result.diagnostics.scopedPages = scoped.length;
  if (!scoped.length) return result;
  const usable = scoped.filter(usablePage);
  const semantic = await retrieveSemanticChunks(root, options.prompt, 8, new Set(usable.map(page => page.id)));
  if (semantic.warning) result.diagnostics.warnings.push(semantic.warning);
  if (semantic.staleEntriesDetected) result.diagnostics.warnings.push("embedding-entry-stale");
  if (usable.length < scoped.length) result.diagnostics.warnings.push("unusable-pages-excluded");
  const crossProjectFrom = options.scope === "semantic" ? options.projectId : undefined;
  const sections = rankDecisionSections(usable, options.prompt, semantic.hits, crossProjectFrom);
  result.diagnostics.matchedSections = sections.length;
  result.evidence = await selectEvidence(root, sections, result.diagnostics.warnings, options.scope === "semantic");
  result.complete = result.evidence.length === sections.length;
  result.followUpPageIds = result.complete ? [] : [...new Set(sections.map(section => section.page.id))];
  if (!sections.length) result.followUpPageIds = noHitPointers(usable, options);
  result.status = result.diagnostics.warnings.length ? "degraded" : result.evidence.length ? "ok" : "no-hit";
  return result;
}

function emptyContext(projectId?: string): TaskContext {
  return { version: 1, projectId: projectId ?? null, status: "no-hit", evidence: [], complete: true, followUpPageIds: [],
    diagnostics: { scopedPages: 0, matchedSections: 0, warnings: [] } };
}

/** Explicit legacy mappings cannot override a different frontmatter owner. */
function inScope(page: ViewerPage, options: TaskContextOptions): boolean {
  if (options.allowedPageIds && !options.allowedPageIds.includes(page.id)) return false;
  if (options.scope === "semantic") return page.pageDirectory === "concepts";
  if (options.projectId) {
    const sources = sourceProjectIds(page.frontmatter);
    if (sources.includes(options.projectId)) return true;
    if (sources.length > 0) return false;
    return options.allowedPageIds !== undefined && page.frontmatter.projectId === undefined;
  }
  return true;
}

function usablePage(page: ViewerPage): boolean {
  return page.freshness.freshnessStatus === "fresh" && !page.freshness.archived && !page.freshness.contradicted;
}

/** A semantic no-hit must not turn unrelated global pages into follow-up recommendations. */
function noHitPointers(pages: ViewerPage[], options: TaskContextOptions): string[] {
  const scoped = options.scope === "semantic"
    ? pages.filter(page => options.projectId && sourceProjectIds(page.frontmatter).includes(options.projectId)) : pages;
  return scoped.map(page => page.id).sort().slice(0, MAX_NO_HIT_PAGE_POINTERS);
}

/** Resolve sources from selected sections, not the first citation on the whole page. */
async function selectEvidence(root: string, sections: DecisionSection[], warnings: string[], semanticScope: boolean): Promise<TaskEvidence[]> {
  const evidence: TaskEvidence[] = [];
  const pages = new Set<string>();
  const budget = createSourceWindowBudget();
  let remaining = MAX_EVIDENCE_CHARS;
  for (const section of sections) {
    if (evidence.length >= MAX_TASK_SECTIONS) break;
    if (!pages.has(section.page.id) && pages.size >= MAX_TASK_PAGES) continue;
    const candidateBudget = { ...budget };
    const item = await resolveSectionEvidence(root, section, candidateBudget, warnings, semanticScope);
    if (!item) continue;
    const size = JSON.stringify(item).length;
    if (size > remaining) continue;
    pages.add(section.page.id);
    evidence.push(item);
    remaining -= size;
    budget.remaining = candidateBudget.remaining;
  }
  return evidence;
}

/** A qualifier cannot provide provenance for an otherwise unsourced decision. */
async function resolveSectionEvidence(root: string, section: DecisionSection,
  budget: ReturnType<typeof createSourceWindowBudget>, warnings: string[], semanticScope: boolean): Promise<TaskEvidence | null> {
  if (!extractClaimCitations(section.text).length) return null;
  const citations = flattenCitations(extractClaimCitations(qualifiedText(section)));
  if (citations.length > budget.remaining) return null;
  const sources = await materializeSourceWindows(root, citations, budget);
  if (sources.length === citations.length) return evidenceForSection(section, sources, semanticScope);
  if (!warnings.includes("unresolved-decision-citation")) warnings.push("unresolved-decision-citation");
  return null;
}

function evidenceForSection(section: DecisionSection, sources: TaskEvidence["sources"], semanticScope: boolean): TaskEvidence {
  const { frontmatter } = section.page;
  const projectIds = sourceProjectIds(frontmatter);
  return { pageId: section.page.id, title: section.page.title, pageRevision: taskPageRevision(section.page),
    updatedAt: typeof frontmatter.updatedAt === "string" ? frontmatter.updatedAt : null,
    decisionObject: typeof frontmatter.knowledgeDecisionObject === "string" ? frontmatter.knowledgeDecisionObject : null,
    section: section.heading, text: section.text, qualifications: section.qualifications ?? "", sources,
    temporalStatus: section.temporalStatus,
    ...(projectIds.length > 0 && (semanticScope || frontmatter.topicScope === "semantic") ? { sourceProjectIds: projectIds } : {}) };
}

function qualifiedText(section: DecisionSection): string {
  return section.qualifications ? `页面适用范围：\n${section.qualifications}\n\n${section.text}` : section.text;
}
