/**
 * Exercise the actual final-gate shell from the workflow with failed, skipped,
 * and cancelled dependencies. A skipped Actions job can otherwise appear green
 * to branch protection. Keep every mandatory job connected to the final gate
 * and ensure PRs to either maintained branch always produce a check.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { load } from "js-yaml";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

interface Workflow {
  on: Record<string, { branches?: string[]; paths?: string[]; "paths-ignore"?: string[] }>;
  env: { NODE_VERSION: string };
  jobs: Record<string, { name: string; if?: string; needs?: string[]; steps: { id?: string; run?: string }[] }>;
}

const workflow = load(readFileSync(".github/workflows/ci.yml", "utf8")) as Workflow;
const gate = workflow.jobs["ci-gate"];
const gateScript = gate.steps.find(step => step.id === "require-success")!.run!;

it("keeps a manually chosen baseline outside the required PR check", () => {
  expect(gate.name).toBe("${{ github.event_name == 'workflow_dispatch' && 'CI Gate (manual)' || 'CI Gate' }}");
});

describe("CI merge gate", () => {
  let directory: string;
  beforeEach(() => { directory = mkdtempSync(path.join(tmpdir(), "llmwiki-ci-gate-")); });
  afterEach(() => { rmSync(directory, { recursive: true, force: true }); });

  it("waits for every mandatory job even when dependencies fail", () => {
    const jobs = Object.keys(workflow.jobs).filter((name) => name !== "ci-gate");
    expect(gate.needs?.slice().sort()).toEqual(jobs.sort());
    expect(gate.if).toBe("${{ always() }}");
  });

  it("runs for maintained branches and merge queues without path filtering", () => {
    for (const event of ["push", "pull_request"]) {
      expect(workflow.on[event].branches).toEqual(["main", "personal/stable"]);
      expect(workflow.on[event].paths).toBeUndefined();
      expect(workflow.on[event]["paths-ignore"]).toBeUndefined();
    }
    expect(workflow.on).toHaveProperty("merge_group");
    const manifest = JSON.parse(readFileSync("package.json", "utf8"));
    expect(workflow.env.NODE_VERSION).toBe(manifest.volta.node);
  });

  it.each(["success", "failure", "cancelled", "skipped"])("handles a %s dependency", (result) => {
    const needs = { healthy: { result: "success" }, dependency: { result } };
    const execution = spawnSync("bash", ["-e", "-o", "pipefail", "-c", gateScript], {
      encoding: "utf8",
      env: { ...process.env, NEEDS_JSON: JSON.stringify(needs),
        GITHUB_STEP_SUMMARY: path.join(directory, "summary") },
    });
    expect(execution.error).toBeUndefined();
    expect(execution.stdout).toContain(`dependency: ${result}`);
    expect(execution.status).toBe(result === "success" ? 0 : 1);
  });

  it("rejects an empty dependency set", () => {
    const result = spawnSync("bash", ["-e", "-o", "pipefail", "-c", gateScript], {
      env: { ...process.env, NEEDS_JSON: "{}", GITHUB_STEP_SUMMARY: path.join(directory, "summary") },
    });
    expect(result.error).toBeUndefined();
    expect(result.status).toBe(1);
  });
});
