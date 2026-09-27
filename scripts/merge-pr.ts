/**
 * Shared PR merge entry point for machines using this private fork without
 * native branch protection. Review SHAs are the caller's attestation, not an
 * automated code-review verdict. Readiness must be proven from GitHub; any
 * missing, stale, unsuccessful, or changing evidence refuses the merge.
 * The merge API atomically matches HEAD only; CI.md documents the base race.
 */
import { execFileSync } from "node:child_process";
import { parseArgs } from "node:util";
import assert from "node:assert/strict";

const SHA = /^[0-9a-f]{40}$/;
const WORKFLOW = ".github/workflows/ci.yml";
const PAGE_SIZE = 100;
const COMMAND_TIMEOUT_MS = 30_000;
const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
// Change this contract together with the workflow's mandatory job matrix.
const REQUIRED_JOBS = [
  "Types and release docs", "Tests (ubuntu-24.04, 1/2)",
  "Tests (ubuntu-24.04, 2/2)", "Tests (macos-15, 1/1)",
  "Hooks and deployment (ubuntu-24.04)", "Hooks and deployment (macos-15)",
  "Codebase health", "CI Gate",
];
const PR_FIELDS = "number,state,isDraft,headRefOid,headRefName,baseRefOid,baseRefName,mergeable,reviewDecision";

interface Review { pr: string; head: string; base: string; dryRun: boolean }
interface PullRequest {
  number: number; state: string; isDraft: boolean; headRefOid: string;
  headRefName: string; baseRefOid: string; baseRefName: string;
  mergeable: string; reviewDecision: string;
}
interface Result { status: string; conclusion: string | null }
interface Run extends Result {
  id: number; head_sha: string; head_branch: string; event: string;
  path: string; run_attempt: number; html_url: string;
}
interface Job extends Result {
  name: string; head_sha: string; run_attempt: number;
  steps: Array<Result & { name: string }>;
}

/** Run tools without a shell, prompts, or unbounded waits. */
function command(binary: string, args: string[]): string {
  return execFileSync(binary, args, {
    encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
    timeout: COMMAND_TIMEOUT_MS, maxBuffer: MAX_OUTPUT_BYTES,
    env: { ...process.env, GH_PROMPT_DISABLED: "1" },
  }).trim();
}

/** JSON parsing and transport failures propagate to the single refusal path. */
function ghJson<T>(args: string[]): T {
  return JSON.parse(command("gh", args));
}

/** Keep every API call on the same explicit GitHub host and repository. */
function api<T>(repo: string, path: string, args: string[] = []): T {
  return ghJson<T>(["api", "--hostname", "github.com", `repos/${repo}/${path}`, ...args]);
}

/** Refuse incomplete review attestations before contacting GitHub. */
function readOptions(): Review {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    "reviewed-head": { type: "string" }, "reviewed-base": { type: "string" },
    "dry-run": { type: "boolean", default: false },
  } });
  const usage = "Usage: npm run pr:merge -- <PR> --reviewed-head <40-char SHA> --reviewed-base <40-char SHA> [--dry-run]";
  assert.equal(positionals.length, 1, usage);
  assert.match(positionals[0], /^[1-9]\d*$/, usage);
  assert(Number.isSafeInteger(Number(positionals[0])), usage);
  const head = values["reviewed-head"] ?? "";
  const base = values["reviewed-base"] ?? "";
  assert.match(head, SHA, usage);
  assert.match(base, SHA, usage);
  return { pr: positionals[0], head, base, dryRun: values["dry-run"] };
}

/** Resolve origin explicitly: gh's implicit fork default can select upstream. */
function originRepository(): string {
  const origin = command("git", ["remote", "get-url", "origin"]);
  const repo = ghJson<{ nameWithOwner: string; url: string }>([
    "repo", "view", origin, "--json", "nameWithOwner,url",
  ]);
  if (repo.url !== `https://github.com/${repo.nameWithOwner}`) {
    throw new Error("The merge guard requires a github.com origin repository.");
  }
  return repo.nameWithOwner;
}

/** Check both live PR metadata and the current target branch tip. */
function currentPR(repo: string, review: Review): PullRequest {
  const pr = ghJson<PullRequest>(["pr", "view", review.pr, "--repo",
    `https://github.com/${repo}`, "--json", PR_FIELDS]);
  const readiness = "PR must be open, ready, mergeable, and free of requested changes.";
  assert.equal(pr.number, Number(review.pr), readiness);
  assert.equal(pr.state, "OPEN", readiness);
  assert.equal(pr.isDraft, false, readiness);
  assert.equal(pr.mergeable, "MERGEABLE", readiness);
  assert.notEqual(pr.reviewDecision, "CHANGES_REQUESTED", readiness);
  assert(["personal/stable", "main"].includes(pr.baseRefName), "PR target must be personal/stable or main, covered by CI.");
  const branch = api<{ object: { sha: string } }>(repo,
    `git/ref/heads/${encodeURIComponent(pr.baseRefName)}`);
  const stale = "HEAD/base changed or differs from the review. Update the branch and review the final commits.";
  assert.equal(pr.headRefOid, review.head, stale);
  assert.equal(pr.baseRefOid, review.base, stale);
  assert.equal(branch.object.sha, review.base, stale);
  return pr;
}

/** Require a branch updated to the reviewed base, independently of CI proof. */
function requireUpdatedBranch(repo: string, review: Review): void {
  const comparison = api<{ merge_base_commit: { sha: string } }>(repo,
    `compare/${review.base}...${review.head}`);
  assert.equal(comparison.merge_base_commit.sha, review.base,
    "PR branch does not contain the reviewed base. Merge the current base into it, then refresh review and CI.");
}

/** Select the newest run before checking its outcome; never seek an older green. */
function latestRun(repo: string, review: Review, pr: PullRequest): Run {
  const response = api<{ total_count: number; workflow_runs: Run[] }>(repo,
    `actions/workflows/ci.yml/runs?event=pull_request&head_sha=${review.head}&per_page=${PAGE_SIZE}`);
  const missing = "Automatic PR CI evidence is missing or the run listing is incomplete.";
  assert(response.total_count > 0, missing);
  assert.equal(response.total_count, response.workflow_runs.length, missing);
  const run = response.workflow_runs.reduce((latest, item) => item.id > latest.id ? item : latest);
  const mismatch = "Latest automatic PR CI is not a successful completed run for this HEAD/branch.";
  assert(Number.isSafeInteger(run.id), mismatch);
  assert(run.id > 0, mismatch);
  assert.equal(run.path, WORKFLOW, mismatch);
  assert.equal(run.event, "pull_request", mismatch);
  assert.equal(run.head_sha, review.head, mismatch);
  assert.equal(run.head_branch, pr.headRefName, mismatch);
  assert(Number.isSafeInteger(run.run_attempt), mismatch);
  assert(run.run_attempt >= 1, mismatch);
  assert.equal(run.status, "completed", mismatch);
  assert.equal(run.conclusion, "success", mismatch);
  return run;
}

/** Inspect every required job in this exact attempt, including the event snapshot. */
function requireJobs(repo: string, review: Review, run: Run): void {
  const { total_count, jobs } = api<{ total_count: number; jobs: Job[] }>(repo,
    `actions/runs/${run.id}/attempts/${run.run_attempt}/jobs?per_page=${PAGE_SIZE}`);
  const incomplete = "CI attempt must contain the complete required job matrix. Partial reruns do not qualify.";
  assert.equal(total_count, REQUIRED_JOBS.length, incomplete);
  assert.deepEqual(jobs.map(job => job.name).sort(), [...REQUIRED_JOBS].sort(), incomplete);
  const failed = "Every required job must succeed in the current CI attempt.";
  for (const job of jobs) {
    assert.equal(job.run_attempt, run.run_attempt, failed);
    assert.equal(job.head_sha, review.head, failed);
    assert.equal(job.status, "completed", failed);
    assert.equal(job.conclusion, "success", failed);
  }
  const context = `PR context #${review.pr} head=${review.head} base=${review.base}`;
  const snapshots = jobs.find(job => job.name === "CI Gate")!.steps.filter(step => step.name === context);
  const untested = "CI has no successful snapshot for this PR and reviewed HEAD/base. Refresh PR CI.";
  assert.equal(snapshots.length, 1, untested);
  assert.equal(snapshots[0].status, "completed", untested);
  assert.equal(snapshots[0].conclusion, "success", untested);
}

/** Use REST's expected-head field without CLI auto-merge or merge-queue behavior. */
function mergePR(repo: string, review: Review): void {
  let merged: { merged: boolean; sha: string; message: string };
  try {
    merged = api(repo, `pulls/${review.pr}/merge`,
      ["--method", "PUT", "-f", `sha=${review.head}`, "-f", "merge_method=merge"]);
  } catch (error) {
    throw new Error("Merge result unconfirmed. Check the PR on GitHub before retrying.", { cause: error });
  }
  assert.equal(merged.merged, true, `GitHub did not confirm a merge: ${merged.message}`);
  assert.match(merged.sha, SHA, "GitHub did not return the merged commit SHA.");
  console.log(`Merged: ${merged.sha}`);
}

/** Final reads narrow races; the mutation atomically rejects a changed HEAD. */
function main(): void {
  const review = readOptions();
  const repo = originRepository();
  const pr = currentPR(repo, review);
  requireUpdatedBranch(repo, review);
  const run = latestRun(repo, review, pr);
  requireJobs(repo, review, run);
  const current = latestRun(repo, review, currentPR(repo, review));
  const changed = "CI changed during verification. Inspect the latest run before trying again.";
  assert.equal(current.id, run.id, changed);
  assert.equal(current.run_attempt, run.run_attempt, changed);
  console.log(`Ready: ${repo}#${review.pr}\nHEAD ${review.head}\nBase ${review.base}\nCI ${run.html_url} (attempt ${run.run_attempt})`);
  if (review.dryRun) { console.log("Dry run: no merge requested."); return; }
  mergePR(repo, review);
}

try { main(); } catch (error) {
  console.error(`Merge stopped: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}
