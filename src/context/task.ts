/** Read-only task evidence: pin a replica, verify project scope, then select sourced sections. */
import { realpath } from "node:fs/promises";
import { buildViewerSnapshot } from "../viewer/snapshot.js";
import type { ViewerPage } from "../viewer/types.js";
import { extractClaimCitations } from "../utils/markdown.js";
import { sourceProjectIds } from "../utils/topic-scope.js";
import { retrieveSemanticChunks, type SemanticRetrievalOutcome } from "./retrieval.js";
import { createSourceWindowBudget, flattenCitations, materializeSourceWindows } from "./provenance.js";
import { decisionSections, taskPageRevision, type DecisionSection } from "./task-sections.js";
import type { PageTaskEvidence, TaskContext, TaskContextOptions, TaskEvidence } from "./task-types.js";
import { readReviewedClaims } from "./ledger.js";
import { rankTaskCandidates, taskSectionCandidate, type TaskCandidate } from "./task-ranking.js";
import { claimCandidate, claimEvidence, scopedClaims, withoutSupersededSections, type TaskSelection } from "./task-claims.js";
import { duplicateEvidence, duplicateSelection, type SelectedEvidence } from "./task-dedup.js";

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
  const ledger = await readReviewedClaims(root);
  if (ledger.warning) result.diagnostics.warnings.push(ledger.warning);
  const scoped = snapshot.pages.filter(page => inScope(page, options));
  result.diagnostics.scopedPages = scoped.length;
  const claims = scopedClaims(ledger.projection, options);
  result.diagnostics.scopedClaims = claims.length;
  const usable = scoped.filter(usablePage);
  const semantic: SemanticRetrievalOutcome = usable.length
    ? await retrieveSemanticChunks(root, options.prompt, 8, new Set(usable.map(page => page.id)))
    : { hits: [], warning: null, staleEntriesDetected: false };
  if (semantic.warning) result.diagnostics.warnings.push(semantic.warning);
  if (semantic.staleEntriesDetected) result.diagnostics.warnings.push("embedding-entry-stale");
  if (usable.length < scoped.length) result.diagnostics.warnings.push("unusable-pages-excluded");
  const crossProjectFrom = options.scope === "semantic" ? options.projectId : undefined;
  const sections = withoutSupersededSections(decisionSections(usable), ledger.projection, options.prompt, result.diagnostics.warnings);
  const candidates: TaskCandidate<TaskSelection>[] = sections.map(section => ({ ...taskSectionCandidate(section, semantic.hits), value: { origin: "page", section } }));
  candidates.push(...claims.map(claimCandidate));
  const ranked = rankTaskCandidates(candidates, options.prompt, crossProjectFrom).map(candidate => candidate.value);
  result.diagnostics.matchedSections = ranked.length;
  const selected = await selectEvidence(root, ranked, result.diagnostics.warnings, options.scope === "semantic");
  result.evidence = selected.evidence;
  setFollowUp(result, selected.selections, usable, options);
  result.status = result.diagnostics.warnings.length ? "degraded" : result.evidence.length ? "ok" : "no-hit";
  return result;
}

/** Keep expandable record references separate from actual page IDs. */
function setFollowUp(result: TaskContext, ranked: TaskSelection[], pages: ViewerPage[], options: TaskContextOptions): void {
  result.complete = result.evidence.length === ranked.length;
  result.followUpPageIds = result.complete ? [] : [...new Set(ranked.flatMap(item => item.origin === "page" ? [item.section.page.id] : []))];
  result.followUpClaimRefs = result.complete ? [] : ranked.flatMap(item => item.origin === "ledger" ? [item.claim.claimRef] : []);
  if (!ranked.length) result.followUpPageIds = noHitPointers(pages, options);
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
async function selectEvidence(root: string, selections: TaskSelection[], warnings: string[], semanticScope: boolean) {
  const selected: SelectedEvidence[] = [];
  const duplicates = new Set<TaskSelection>();
  for (const selection of selections) {
    if (!canConsider(selection, selected)) continue;
    const budget = createSourceWindowBudget();
    budget.remaining -= selected.reduce((total, item) => total + item.evidence.sources.length, 0);
    const evidence = await resolveSelectionEvidence(root, selection, budget, warnings, semanticScope);
    if (!evidence) continue;
    const candidate = { selection, evidence };
    const previous = selected.find(item => duplicateEvidence(item, candidate));
    if (previous && evidenceSize(previous.evidence) <= evidenceSize(evidence)) { duplicates.add(selection); continue; }
    const retained = selected.filter(item => item !== previous);
    if (!fitsEvidenceBudget([...retained.map(item => item.evidence), evidence])) continue;
    if (previous) { duplicates.add(previous.selection); selected[selected.indexOf(previous)] = candidate; }
    else selected.push(candidate);
  }
  return { evidence: selected.map(item => item.evidence), selections: selections.filter(item => !duplicates.has(item)) };
}

/** Once full, only an equivalent representation can replace an existing slot. */
function canConsider(selection: TaskSelection, selected: SelectedEvidence[]): boolean {
  if (selected.some(item => duplicateSelection(item.selection, selection))) return true;
  if (selected.length >= MAX_TASK_SECTIONS) return false;
  const pages = new Set(selected.flatMap(item => item.evidence.origin === "ledger" ? [] : [item.evidence.pageId]));
  return selection.origin === "ledger" || pages.has(selection.section.page.id) || pages.size < MAX_TASK_PAGES;
}

/** Recompute the small selected set so replacing a duplicate refunds all of its budgets. */
function fitsEvidenceBudget(evidence: TaskEvidence[]): boolean {
  const pages = new Set(evidence.flatMap(item => item.origin === "ledger" ? [] : [item.pageId]));
  return evidence.length <= MAX_TASK_SECTIONS && pages.size <= MAX_TASK_PAGES
    && evidence.reduce((total, item) => total + evidenceSize(item), 0) <= MAX_EVIDENCE_CHARS;
}

function evidenceSize(evidence: TaskEvidence): number { return JSON.stringify(evidence).length; }

/** Each origin resolves through its own provenance boundary before consuming the shared budget. */
async function resolveSelectionEvidence(root: string, selection: TaskSelection,
  budget: ReturnType<typeof createSourceWindowBudget>, warnings: string[], semanticScope: boolean): Promise<TaskEvidence | null> {
  return selection.origin === "ledger" ? claimEvidence(selection.claim)
    : resolveSectionEvidence(root, selection.section, budget, warnings, semanticScope);
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

function evidenceForSection(section: DecisionSection, sources: TaskEvidence["sources"], semanticScope: boolean): PageTaskEvidence {
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
