/** Executable report entry shared by the CLI harness and repository tests. */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { runDecisionReuseEvaluation, type DecisionReuseReport } from "./fixtures/decision-reuse-runner.js";

describe("synthetic decision reuse baseline", () => {
  it("runs every case through task and hook retrieval and emits a reviewable report", async () => {
    const report = await runDecisionReuseEvaluation();
    const reportPath = process.env.DECISION_REUSE_REPORT;
    if (reportPath) {
      await mkdir(path.dirname(reportPath), { recursive: true });
      await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    }
    const failed = report.rows.filter(row => !row.task.pass || !row.hook.pass).map(row => row.id);
    console.info("DECISION_REUSE_BASELINE", JSON.stringify({
      fixture: report.fixture, cases: report.caseCount, taskExactSetPass: report.taskExactSetPass,
      exactSetDenominator: report.taskExactSetDenominator,
      hookExactSetPass: report.hookExactSetPass, bothExactSetPass: report.bothExactSetPass,
      taskExpectedRecallRows: report.taskExpectedRecallRows, hookExpectedRecallRows: report.hookExpectedRecallRows,
      expectedRecallDenominator: report.taskExpectedRecallDenominator,
      taskForbiddenContaminationRows: report.taskForbiddenContaminationRows,
      hookForbiddenContaminationRows: report.hookForbiddenContaminationRows,
      forbiddenDenominator: report.taskForbiddenContaminationDenominator,
      taskNoEvidenceFailures: report.taskNoEvidenceFailures, hookNoEvidenceFailures: report.hookNoEvidenceFailures,
      noEvidenceDenominator: report.taskNoEvidenceDenominator,
      failed,
    }));
    expect(report.caseCount).toBe(37);
    expect(mandatoryContractFailures(report)).toEqual([]);
  }, 60_000);
});

function mandatoryContractFailures(report: DecisionReuseReport): string[] {
  return report.rows.flatMap(contractFailuresForRow);
}

function contractFailuresForRow(row: DecisionReuseReport["rows"][number]): string[] {
  return [...forbiddenFailures(row), ...noEvidenceFailures(row), ...requiredRecallFailures(row)];
}

function forbiddenFailures(row: DecisionReuseReport["rows"][number]): string[] {
  return row.task.forbiddenHits.length || row.hook.forbiddenHits.length ? [`${row.id}:forbidden`] : [];
}

function noEvidenceFailures(row: DecisionReuseReport["rows"][number]): string[] {
  return row.mustHaveNoEvidence && (row.task.actual.length > 0 || row.hook.actual.length > 0)
    ? [`${row.id}:no-evidence`] : [];
}

function requiredRecallFailures(row: DecisionReuseReport["rows"][number]): string[] {
  const mustRecall = row.scope === "semantic" || row.id.startsWith("shared-principle-") ||
    row.id === "timeline-history-survives-follow-up";
  return mustRecall && (!row.task.expectedRecall || !row.hook.expectedRecall) ? [`${row.id}:recall`] : [];
}
