/**
 * Exercise the actual merge CLI against an isolated Git repository and a fake
 * gh executable. GitHub responses are the external boundary; the guard itself
 * runs unchanged. Every denial must leave the mutation log empty, while success
 * must send an atomic expected-HEAD merge with no admin or auto-merge bypass.
 */
import { execFileSync, spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, expect, it } from "vitest";
import yaml from "js-yaml";

const HEAD = "a".repeat(40);
const BASE = "b".repeat(40);
const OTHER = "c".repeat(40);
const REPO = "example/private-fork";
const URL = `https://github.com/${REPO}`;
const COMMAND = path.resolve("scripts/merge-pr.ts");
const PR_FIELDS = "number,state,isDraft,headRefOid,headRefName,baseRefOid,baseRefName,mergeable,reviewDecision";
const JOB_NAMES = ["Types and release docs", "Tests (ubuntu-24.04, 1/2)",
  "Tests (ubuntu-24.04, 2/2)", "Tests (macos-15, 1/1)",
  "Hooks and deployment (ubuntu-24.04)", "Hooks and deployment (macos-15)",
  "Codebase health", "CI Gate"];
let root: string;
let state: ReturnType<typeof responses>;

/** A complete successful PR run, using real Actions API field names. */
function responses() {
  const pr = { number: 11, state: "OPEN", isDraft: false, headRefOid: HEAD,
    headRefName: "codex/change", baseRefOid: BASE, baseRefName: "personal/stable",
    mergeable: "MERGEABLE", reviewDecision: "" };
  const run = { id: 100, path: ".github/workflows/ci.yml", event: "pull_request",
    head_sha: HEAD, head_branch: pr.headRefName, run_attempt: 1,
    status: "completed", conclusion: "success", html_url: `${URL}/actions/runs/100` };
  const jobs = JOB_NAMES.map(name => ({ name, head_sha: HEAD, run_attempt: 1,
    status: "completed", conclusion: "success", steps: name === "CI Gate"
      ? [{ name: `PR context #11 head=${HEAD} base=${BASE}`, status: "completed", conclusion: "success" }] : [] }));
  return { pr, finalPR: structuredClone(pr), branch: { object: { sha: BASE } }, finalBranch: { object: { sha: BASE } },
    compare: { merge_base_commit: { sha: BASE } },
    runs: { total_count: 1, workflow_runs: [run] }, finalRuns: { total_count: 1, workflow_runs: [structuredClone(run)] },
    jobs: { total_count: jobs.length, jobs }, merge: { merged: true, sha: OTHER, message: "Merged" } };
}

beforeEach(async () => {
  state = responses();
  root = await mkdtemp(path.join(tmpdir(), "llmwiki-pr-merge-"));
  execFileSync("git", ["init", "-q"], { cwd: root });
  execFileSync("git", ["remote", "add", "origin", `${URL}.git`], { cwd: root });
  await mkdir(path.join(root, "bin"));
  await writeFile(path.join(root, "mutations.jsonl"), "");
  await writeFile(path.join(root, "bin", "gh"), fakeGh(), { mode: 0o755 });
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });

/** Strict fake transport: unexpected requests fail instead of inventing data. */
function fakeGh(): string {
  return `#!/usr/bin/env node
/** Isolated GitHub transport for the merge guard's process tests. */
const fs = require("node:fs");
const args = process.argv.slice(2);
if (args.includes("PUT")) fs.appendFileSync("mutations.jsonl", JSON.stringify(args) + "\\n");
const replies = JSON.parse(fs.readFileSync("replies.json", "utf8"));
const reply = replies[JSON.stringify(args)]?.shift();
fs.writeFileSync("replies.json", JSON.stringify(replies));
if (!reply) { console.error("Unexpected gh request: " + JSON.stringify(args)); process.exit(99); }
if (reply.error) { console.error(reply.error); process.exit(1); }
console.log(JSON.stringify(reply.data));
`;
}

/** Feed snapshots in request order, then run the real CLI without networking. */
async function run(args: string[] = [], apiError?: "compare" | "merge") {
  const replies: Record<string, Array<{ data?: unknown; error?: string }>> = {};
  const add = (command: string[], ...data: unknown[]) => {
    replies[JSON.stringify(command)] = data.map(value => ({ data: value }));
  };
  const api = (endpoint: string) => ["api", "--hostname", "github.com", `repos/${REPO}/${endpoint}`];
  add(["repo", "view", `${URL}.git`, "--json", "nameWithOwner,url"], { nameWithOwner: REPO, url: URL });
  add(["pr", "view", "11", "--repo", URL, "--json", PR_FIELDS], state.pr, state.finalPR);
  add(api("git/ref/heads/personal%2Fstable"), state.branch, state.finalBranch);
  add(api(`compare/${BASE}...${HEAD}`), state.compare);
  add(api(`actions/workflows/ci.yml/runs?event=pull_request&head_sha=${HEAD}&per_page=100`), state.runs, state.finalRuns);
  const attempt = state.runs.workflow_runs.at(-1)?.run_attempt;
  add(api(`actions/runs/100/attempts/${attempt}/jobs?per_page=100`), state.jobs);
  const merge = [...api("pulls/11/merge"), "--method", "PUT", "-f", `sha=${HEAD}`, "-f", "merge_method=merge"];
  add(merge, state.merge);
  if (apiError) {
    const failed = apiError === "merge" ? merge : api(`compare/${BASE}...${HEAD}`);
    replies[JSON.stringify(failed)] = [{ error: "API unavailable" }];
  }
  await writeFile(path.join(root, "replies.json"), JSON.stringify(replies));
  return spawnSync(process.execPath, [COMMAND, "11", "--reviewed-head", HEAD, "--reviewed-base", BASE, ...args], {
    cwd: root, encoding: "utf8", timeout: 30_000,
    env: { ...process.env, PATH: `${path.join(root, "bin")}${path.delimiter}${process.env.PATH}` },
  });
}

/** Refusal is observable as nonzero exit, an explanation, and no merge request. */
async function refused(reason: string, args: string[] = []): Promise<void> {
  const result = await run(args);
  expect(result.error).toBeUndefined();
  expect(result.status).toBe(1);
  expect(result.stderr).toContain(reason);
  expect(await readFile(path.join(root, "mutations.jsonl"), "utf8")).toBe("");
}

it("dry-runs a ready PR without sending any mutation", async () => {
  const result = await run(["--dry-run"]);
  expect(result.status, result.stderr).toBe(0);
  expect(result.stdout).toContain(`Ready: ${REPO}#11`);
  expect(result.stdout).toContain("Dry run: no merge requested.");
  expect(await readFile(path.join(root, "mutations.jsonl"), "utf8")).toBe("");
});

it("merges only into origin using an atomic expected HEAD and merge commit", async () => {
  const result = await run();
  expect(result.status, result.stderr).toBe(0);
  expect(result.stdout).toContain(`Merged: ${OTHER}`);
  const mutations = (await readFile(path.join(root, "mutations.jsonl"), "utf8")).trim().split("\n");
  expect(mutations.map(line => JSON.parse(line))).toEqual([["api", "--hostname", "github.com",
    `repos/${REPO}/pulls/11/merge`, "--method", "PUT", "-f", `sha=${HEAD}`, "-f", "merge_method=merge"]]);
});

it.each(["CLOSED", "MERGED"])("rejects a %s PR", async value => {
  state.pr.state = value;
  await refused("PR must be open");
});

it.each(["isDraft", "mergeable", "reviewDecision"])("rejects an unready %s", async field => {
  if (field === "isDraft") state.pr.isDraft = true;
  if (field === "mergeable") state.pr.mergeable = "UNKNOWN";
  if (field === "reviewDecision") state.pr.reviewDecision = "CHANGES_REQUESTED";
  await refused("PR must be open");
});

it("rejects a target branch outside the automatic CI contract", async () => {
  state.pr.baseRefName = "feature/unprotected";
  await refused("PR target must be");
});

it.each(["headRefOid", "baseRefOid"] as const)("rejects stale reviewed %s", async field => {
  state.pr[field] = OTHER;
  await refused("HEAD/base changed");
});

it("rejects a moved branch tip even if PR metadata is stale", async () => {
  state.branch.object.sha = OTHER;
  await refused("HEAD/base changed");
});

it("rejects a branch tip moving during verification even with unchanged PR metadata", async () => {
  state.finalBranch.object.sha = OTHER;
  await refused("HEAD/base changed");
});

it("requires the PR to contain its current target branch", async () => {
  state.compare.merge_base_commit.sha = OTHER;
  await refused("does not contain the reviewed base");
});

it.each(["failure", "cancelled", "skipped", "timed_out", "action_required"])("rejects latest CI outcome %s instead of an older green", async conclusion => {
  const latest = state.runs.workflow_runs[0];
  latest.conclusion = conclusion;
  state.runs.workflow_runs.unshift({ ...latest, id: 99, conclusion: "success" });
  state.runs.total_count = 2;
  await refused("Latest automatic PR CI");
});

it("rejects a CI run still in progress", async () => {
  state.runs.workflow_runs[0].status = "in_progress";
  await refused("Latest automatic PR CI");
});

it.each(["path", "event", "head_sha", "head_branch"] as const)("rejects CI from the wrong %s", async field => {
  state.runs.workflow_runs[0][field] = "unrelated";
  await refused("Latest automatic PR CI");
});

it.each([0, 2])("rejects missing or incomplete run evidence (%i)", async count => {
  state.runs.total_count = count;
  await refused("evidence is missing");
});

it.each(["missing", "duplicate", "extra"])("rejects a %s matrix job", async mode => {
  if (mode === "missing") state.jobs.jobs.pop();
  if (mode === "duplicate") state.jobs.jobs[0].name = "CI Gate";
  if (mode === "extra") state.jobs.jobs.push({ ...state.jobs.jobs[0], name: "unexpected" });
  state.jobs.total_count = state.jobs.jobs.length;
  await refused("complete required job matrix");
});

it.each(["failure", "cancelled", "skipped"])("rejects a %s job despite a green run", async outcome => {
  state.jobs.jobs[0].conclusion = outcome;
  await refused("Every required job must succeed");
});

it.each(["head_sha", "status"] as const)("rejects a job with incorrect %s", async field => {
  state.jobs.jobs[0][field] = "unverified";
  await refused("Every required job must succeed");
});

it("rejects jobs carried over from an earlier attempt", async () => {
  state.runs.workflow_runs[0].run_attempt = 2;
  await refused("Every required job must succeed");
});

it("accepts a successful full rerun with a matching context", async () => {
  state.runs.workflow_runs[0].run_attempt = 2;
  state.finalRuns.workflow_runs[0].run_attempt = 2;
  state.jobs.jobs.forEach(job => { job.run_attempt = 2; });
  expect((await run(["--dry-run"])).status).toBe(0);
});

it.each(["missing", "base", "PR", "skipped", "duplicate"])("rejects a %s CI event snapshot", async mode => {
  const gate = state.jobs.jobs.find(job => job.name === "CI Gate")!;
  if (mode === "missing") gate.steps = [];
  if (mode === "base") gate.steps[0].name = `PR context #11 head=${HEAD} base=${OTHER}`;
  if (mode === "PR") gate.steps[0].name = `PR context #12 head=${HEAD} base=${BASE}`;
  if (mode === "skipped") gate.steps[0].conclusion = "skipped";
  if (mode === "duplicate") gate.steps.push({ ...gate.steps[0] });
  await refused("no successful snapshot");
});

it.each(["headRefOid", "baseRefOid"] as const)("rejects %s changing during verification", async field => {
  state.finalPR[field] = OTHER;
  await refused("HEAD/base changed");
});

it.each(["id", "run_attempt"] as const)("rejects a new CI %s during verification", async field => {
  state.finalRuns.workflow_runs[0][field] += 1;
  await refused("CI changed during verification");
});

it("does not merge when the API is unavailable", async () => {
  const result = await run([], "compare");
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("API unavailable");
  expect(await readFile(path.join(root, "mutations.jsonl"), "utf8")).toBe("");
});

it("reports an unknown outcome after a merge transport failure without retrying", async () => {
  const result = await run([], "merge");
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("Merge result unconfirmed. Check the PR on GitHub before retrying.");
  expect(result.stdout).not.toContain("Merged:");
  expect((await readFile(path.join(root, "mutations.jsonl"), "utf8")).trim().split("\n")).toHaveLength(1);
});

it("does not claim success when GitHub rejects the final merge", async () => {
  state.merge.merged = false;
  state.merge.message = "Head changed";
  const result = await run();
  expect(result.status).toBe(1);
  expect(result.stderr).toContain("GitHub did not confirm a merge: Head changed");
  expect(result.stdout).not.toContain("Merged:");
});

it.each([["--reviewed-head", "short"], ["--reviewed-base", ""], ["--admin"]])("rejects invalid arguments %j", async (...args) => {
  await refused("Merge stopped:", args);
});

it("keeps the required matrix and PR snapshot aligned with the real workflow", async () => {
  const workflow = yaml.load(await readFile(".github/workflows/ci.yml", "utf8")) as {
    jobs: Record<string, { name: string; strategy?: { matrix: { include?: Array<{ os: string; shard: string }>; os?: string[] } };
      steps: Array<{ name?: string; if?: string; run?: string }> }>;
  };
  const names = Object.entries(workflow.jobs).flatMap(([id, job]) => {
    const matrix = job.strategy?.matrix;
    if (matrix?.include) return matrix.include.map(row => job.name.replace("${{ matrix.os }}", row.os).replace("${{ matrix.shard }}", row.shard));
    if (matrix?.os) return matrix.os.map(os => job.name.replace("${{ matrix.os }}", os));
    return [id === "ci-gate" ? "CI Gate" : job.name];
  });
  expect(names.sort()).toEqual([...JOB_NAMES].sort());
  expect(workflow.jobs["ci-gate"].steps).toContainEqual({
    name: "PR context #${{ github.event.pull_request.number }} head=${{ github.event.pull_request.head.sha }} base=${{ github.event.pull_request.base.sha }}",
    if: "github.event_name == 'pull_request'", run: "true",
  });
});
