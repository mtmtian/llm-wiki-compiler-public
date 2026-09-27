/** Run the same synthetic queries through task-context and native-hook retrieval. */
import { rm } from "node:fs/promises";
import { buildHookContext } from "../../extensions/knowledge-flow/context.js";
import { buildTaskContext } from "../../src/context/task.js";
import { DECISION_REUSE_CASES, DECISION_REUSE_PAGES, type DecisionReuseCase } from "./decision-reuse-corpus.js";
import { addDecisionReuseFollowUp, seedDecisionReuseWiki } from "./decision-reuse-wiki.js";
import { makeTempRoot } from "./temp-root.js";

const RETRIEVAL_DESCRIPTION = "real task + hook contexts; fixture has no embedding index, so lexical fallback is used";

interface EvidenceRecord {
  pageId: string;
  section: string;
}

interface RouteResult {
  pass: boolean;
  expectedRecall: boolean;
  missingExpected: string[];
  unexpected: string[];
  forbiddenHits: string[];
  actual: string[];
}

export interface DecisionReuseRow {
  id: string;
  phase: "initial" | "after-follow-up";
  scope: "project" | "semantic";
  acceptance: "exact-evidence" | "project-boundary";
  mustHaveNoEvidence: boolean;
  prompt: string;
  expected: string[];
  task: RouteResult & { status: string; warnings: string[] };
  hook: RouteResult & { status: string; complete: boolean };
  forbiddenPageIds: string[];
  repeat?: { firstChars: number; secondChars: number; summarizedRepeat: boolean };
}

export interface DecisionReuseReport {
  fixture: "synthetic-chinese-decision-reuse-v1";
  sourceRevision: string;
  retrieval: typeof RETRIEVAL_DESCRIPTION;
  caseCount: number;
  taskExactSetPass: number;
  taskExactSetDenominator: number;
  hookExactSetPass: number;
  hookExactSetDenominator: number;
  bothExactSetPass: number;
  taskExpectedRecallRows: number;
  taskExpectedRecallDenominator: number;
  hookExpectedRecallRows: number;
  hookExpectedRecallDenominator: number;
  taskForbiddenContaminationRows: number;
  taskForbiddenContaminationDenominator: number;
  hookForbiddenContaminationRows: number;
  hookForbiddenContaminationDenominator: number;
  taskNoEvidenceFailures: number;
  taskNoEvidenceDenominator: number;
  hookNoEvidenceFailures: number;
  hookNoEvidenceDenominator: number;
  rows: DecisionReuseRow[];
}

/** Build one report while applying the follow-up material at its declared phase. */
export async function runDecisionReuseEvaluation(): Promise<DecisionReuseReport> {
  const root = await makeTempRoot("decision-reuse-eval");
  try {
    const wiki = await seedDecisionReuseWiki(root);
    const rows: DecisionReuseRow[] = [];
    let followUpAdded = false;
    for (const scenario of DECISION_REUSE_CASES) {
      if (scenario.phase === "after-follow-up" && !followUpAdded) {
        await addDecisionReuseFollowUp(wiki);
        followUpAdded = true;
      }
      rows.push(await evaluateCase(root, scenario));
    }
    return buildReport(rows);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

async function evaluateCase(root: string, scenario: DecisionReuseCase): Promise<DecisionReuseRow> {
  const allowedPageIds = DECISION_REUSE_PAGES.map(page => `concepts/${page.slug}`);
  const scope = scenario.scope ?? "project";
  const taskContext = await buildTaskContext({ root, scope, projectId: scenario.projectId, prompt: scenario.prompt, allowedPageIds });
  const hookInput = { config: { wikiRoot: root, maxContextChars: 5000,
      ...(scope === "semantic" ? { topicScope: "semantic" as const } : {}) }, projectId: scenario.projectId,
    prompt: scenario.prompt, allowedPageIds, seen: {} };
  const hookContext = await buildHookContext(hookInput);
  const taskRecords = taskContext.evidence.map(item => ({ pageId: item.pageId, section: item.section }));
  const hookRecords = parseHookEvidence(hookContext.context);
  const expected = scenario.expected.map(identify);
  const forbidden = scenario.forbiddenPageIds ?? [];
  const task: DecisionReuseRow["task"] = { ...routeResult(taskRecords, scenario, forbidden),
    status: taskContext.status, warnings: [...taskContext.diagnostics.warnings] };
  const hook: DecisionReuseRow["hook"] = { ...routeResult(hookRecords, scenario, forbidden),
    status: hookContext.status, complete: hookContext.complete };
  const repeat = scenario.repeat ? await repeatedTurn(hookInput, hookContext.seen, hookContext.context.length) : undefined;
  return { id: scenario.id, phase: scenario.phase ?? "initial", scope,
    acceptance: scenario.acceptance ?? "exact-evidence", prompt: scenario.prompt, expected,
    mustHaveNoEvidence: scenario.expectNoEvidence ?? false, task, hook, forbiddenPageIds: forbidden,
    ...(repeat ? { repeat } : {}) };
}

function routeResult(actual: EvidenceRecord[], scenario: DecisionReuseCase, forbidden: string[]): RouteResult {
  const expected = scenario.expected.map(identify);
  const found = new Set(actual.map(identify));
  const boundaryOnly = scenario.acceptance === "project-boundary";
  const missingExpected = boundaryOnly ? [] : expected.filter(item => !found.has(item));
  const unexpected = actual.map(identify).filter(item =>
    (!boundaryOnly && !expected.includes(item)) || forbidden.some(id => item.startsWith(`${id}#`)));
  const forbiddenHits = actual.map(identify).filter(item => forbidden.some(id => item.startsWith(`${id}#`)));
  const noEvidencePass = !scenario.expectNoEvidence || actual.length === 0;
  const expectedRecall = missingExpected.length === 0;
  return { pass: expectedRecall && unexpected.length === 0 && forbiddenHits.length === 0 && noEvidencePass,
    expectedRecall, missingExpected, unexpected, forbiddenHits, actual: actual.map(identify) };
}

function parseHookEvidence(context: string): EvidenceRecord[] {
  const records: EvidenceRecord[] = [];
  const pattern = /【([^】]+)】\n页 ([^ ·\n]+)/g;
  for (const match of context.matchAll(pattern)) records.push({ section: match[1], pageId: match[2] });
  return records;
}

async function repeatedTurn(input: Parameters<typeof buildHookContext>[0], seen: Record<string, string>, firstChars: number) {
  const repeated = await buildHookContext({ ...input, seen });
  return { firstChars, secondChars: repeated.context.length,
    summarizedRepeat: repeated.context.includes("已提供：") };
}

function identify(record: EvidenceRecord): string {
  return `${record.pageId}#${record.section}`;
}

function buildReport(rows: DecisionReuseRow[]): DecisionReuseReport {
  const exactCases = rows.filter(row => row.acceptance === "exact-evidence");
  const expectedCases = rows.filter(row => row.expected.length > 0);
  const contaminationCases = rows.filter(row => row.forbiddenPageIds.length > 0);
  const noEvidenceCases = rows.filter(row => row.mustHaveNoEvidence);
  const taskExactSetPass = exactCases.filter(row => row.task.pass).length;
  const hookExactSetPass = exactCases.filter(row => row.hook.pass).length;
  return { fixture: "synthetic-chinese-decision-reuse-v1",
    sourceRevision: process.env.DECISION_REUSE_SOURCE_REVISION ?? "working-tree",
    retrieval: RETRIEVAL_DESCRIPTION,
    caseCount: rows.length, taskExactSetPass, taskExactSetDenominator: exactCases.length,
    hookExactSetPass, hookExactSetDenominator: exactCases.length,
    bothExactSetPass: exactCases.filter(row => row.task.pass && row.hook.pass).length,
    taskExpectedRecallRows: expectedCases.filter(row => row.task.expectedRecall).length,
    taskExpectedRecallDenominator: expectedCases.length,
    hookExpectedRecallRows: expectedCases.filter(row => row.hook.expectedRecall).length,
    hookExpectedRecallDenominator: expectedCases.length,
    taskForbiddenContaminationRows: rows.filter(row => row.task.forbiddenHits.length > 0).length,
    taskForbiddenContaminationDenominator: contaminationCases.length,
    hookForbiddenContaminationRows: rows.filter(row => row.hook.forbiddenHits.length > 0).length,
    hookForbiddenContaminationDenominator: contaminationCases.length,
    taskNoEvidenceFailures: noEvidenceCases.filter(row => row.task.actual.length > 0).length,
    taskNoEvidenceDenominator: noEvidenceCases.length,
    hookNoEvidenceFailures: noEvidenceCases.filter(row => row.hook.actual.length > 0).length,
    hookNoEvidenceDenominator: noEvidenceCases.length,
    rows };
}
