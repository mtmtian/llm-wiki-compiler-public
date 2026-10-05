/** Exercise the actual worker process, including transport aborts and piped JSON delivery. */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { build } from "tsup";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { trialStatus } from "../extensions/knowledge-flow/jev-ledger.js";
import { GAME_PAGE, GAME_PROJECT, LANGUAGE_DECISION, LANGUAGE_QUESTION, useTaskWikiRoot } from "./fixtures/task-context-wiki.js";
import { unresponsiveHttpServer } from "./fixtures/unresponsive-http.js";

const wiki = useTaskWikiRoot("worker-process");
let runtime: string;
beforeAll(async () => {
  runtime = await mkdtemp(path.join(tmpdir(), "wiki-process-runtime-"));
  await symlink(path.resolve("node_modules"), path.join(runtime, "node_modules"), "dir");
  await build({ entry: { worker: "extensions/knowledge-flow/entry.ts" }, outDir: runtime,
    platform: "node", format: ["esm"], target: "node24", bundle: true, dts: false, config: false,
    removeNodeProtocol: false, outExtension: () => ({ js: ".mjs" }), silent: true });
});
afterAll(async () => { await rm(runtime, { recursive: true, force: true }); });

function input() {
  return { config: { wikiRoot: wiki.value, stateDir: wiki.value, maxContextChars: 6000 },
    projectId: GAME_PROJECT, prompt: LANGUAGE_QUESTION, allowedPageIds: [GAME_PAGE], seen: {} };
}

/** Source and state stay isolated; empty evidence and corrupt cache cases must never call a model. */
function processingInput(stateName: string) {
  return { config: { wikiRoot: wiki.value, stateDir: path.join(wiki.value, stateName),
    model: "test", maxProposals: 5, maxPendingPerProject: 1 },
  job: { id: "process-boundary", projectId: "example", projectLabel: "Example", sessionId: "session", turnId: "turn",
    cwd: wiki.value, createdAt: "2025-01-01T00:00:00Z", prompt: "Synthetic processing boundary", lastAssistant: "",
    evidence: [] as Array<{ id: string; kind: "user"; text: string; sha256: string; observedAt: string; locator: string }>,
    allowedPageIds: [] as string[] } };
}

/** The real hook's four-second deadline bounds both output and process completion. */
function run(operation: string, payload: object, preload?: string, timeout = 4000) {
  const args = [...(preload ? ["--import", pathToFileURL(preload).href] : []), path.join(runtime, "worker.mjs"), operation];
  const child = spawn(process.execPath, args, { env: { ...process.env, TYPESAFE_API_KEY: "local-test-only",
    LLMWIKI_EMBEDDING_PROVIDER: "none", NODE_OPTIONS: "" }, stdio: ["pipe", "pipe", "pipe"] });
  const chunks: Buffer[] = [];
  let stderr = "";
  const timer = setTimeout(() => child.kill("SIGKILL"), timeout);
  child.stdout.on("data", chunk => chunks.push(chunk));
  child.stderr.on("data", chunk => { stderr += chunk; });
  child.stdin.on("error", () => { /* Process failure is asserted from close, including stderr. */ });
  child.stdin.end(JSON.stringify(payload));
  return new Promise<{ code: number | null; signal: string | null; stdout: string; stderr: string }>((resolve, reject) => {
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", (code, signal) => {
      clearTimeout(timer); resolve({ code, signal, stdout: Buffer.concat(chunks).toString("utf8"), stderr });
    });
  });
}

describe("single-request context process", () => {
  it("Given an aborted provider with a lingering handle, delivers fallback and settles before the hook deadline", async () => {
    const server = await unresponsiveHttpServer();
    const preload = path.join(wiki.value, "transport.mjs");
    await writeFile(preload, `const nativeFetch = globalThis.fetch;
globalThis.fetch = async (_url, init) => {
  try { return await nativeFetch(${JSON.stringify(server.url)}, init); }
  catch (error) { setTimeout(() => {}, 10000); throw error; }
};`);
    try {
      // Two relevant current decisions ensure this tests transport fallback, not historical noise.
      const request = { ...input(), prompt: "小游戏存档保留与当前语言范围分别怎么规定？" };
      const result = await run("context", { ...request, config: { ...request.config,
        jevContext: { enabled: true, budgetUsd: .02, expiresAt: "2030-01-01T00:00:00Z" } } }, preload);
      expect(result.signal, result.stderr).toBeNull();
      expect(result.code, result.stderr).toBe(0);
      const output = JSON.parse(result.stdout);
      expect(output.context).toContain(LANGUAGE_DECISION);
      expect(output.context).toContain("保留玩家存档");
      expect(output.references.length).toBeGreaterThanOrEqual(2);
      expect(output.diagnostics.contextRanking).toEqual({ mode: "rules", reason: "transport-failure" });
      expect(trialStatus(wiki.value)).toMatchObject({ requests: 1, fallbacks: 1, pending: {} });
    } finally { await server.close(); }
  });

  it("Given JSON larger than the pipe buffer, flushes the complete result before exiting", async () => {
    const seen = Object.fromEntries(Array.from({ length: 4000 }, (_, i) => [`page-${i}`, "revision".repeat(8)]));
    const result = await run("context", { ...input(), seen });
    expect(result.code, result.stderr).toBe(0);
    expect(Buffer.byteLength(result.stdout)).toBeGreaterThan(200_000);
    expect(JSON.parse(result.stdout).seen).toMatchObject(seen);
  });

  it("Given a non-context operation, retains natural shutdown and completes pending callbacks", async () => {
    const marker = path.join(wiki.value, "settled.txt");
    const preload = path.join(wiki.value, "pending.mjs");
    await writeFile(preload, `import { writeFileSync } from "node:fs";
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = (...args) => {
  setTimeout(() => writeFileSync(${JSON.stringify(marker)}, "settled"), 50);
  return write(...args);
};`);
    const result = await run("resolve", { config: { stateDir: wiki.value }, jobId: "absent", action: "dismiss" }, preload);
    expect(result.code, result.stderr).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({ resolved: false });
    expect(await readFile(marker, "utf8")).toBe("settled");
  });
});

describe("queue result process contract", () => {
  it("Given full review capacity, Then serializes a recoverable defer and resumes the same ID after release", async () => {
    const request = processingInput("capacity-state");
    const review = path.join(request.config.stateDir, "review", "held.json");
    await mkdir(path.dirname(review), { recursive: true });
    await writeFile(review, JSON.stringify({ jobId: "held", projectId: "example" }));
    const deferred = await run("process", request);
    expect(deferred.code, deferred.stderr).toBe(0);
    expect(JSON.parse(deferred.stdout)).toMatchObject({ status: "deferred", reviewCount: 0 });
    await expect(readdir(path.join(request.config.stateDir, "audit"))).rejects.toThrow();
    await rm(review);
    const resumed = await run("process", request);
    expect(resumed.code, resumed.stderr).toBe(0);
    expect(JSON.parse(resumed.stdout)).toMatchObject({ status: "empty", reviewCount: 0 });
  });

  it("Given corrupt frozen model JSON, Then transports and replays a permanent error without a human hold", async () => {
    const request = processingInput("invalid-state");
    const text = "The user confirms a synthetic rule.";
    const hash = (value: string) => createHash("sha256").update(value).digest("hex");
    request.job.evidence.push({ id: "source", kind: "user", text, sha256: hash(text),
      observedAt: request.job.createdAt, locator: "synthetic://source" });
    const directory = path.join(request.config.stateDir, "consolidation", hash(request.job.id));
    await mkdir(directory, { recursive: true });
    await writeFile(path.join(directory, "knowledge_topic_plan.json"), "{");
    const payload = { config: { ...request.config, exchange: { protocolVersion: 2 } },
      job: { ...request.job, sessionContext: { version: 1, revision: 1, summary: "", topicPageIds: [], evidence: [] } } };
    const first = await run("process", payload);
    expect(first.code, first.stderr).toBe(0);
    expect(JSON.parse(first.stdout)).toMatchObject({ status: "error", retryable: false, reviewCount: 0 });
    const repeated = await run("process", payload);
    expect(repeated.code, repeated.stderr).toBe(0);
    expect(JSON.parse(repeated.stdout)).toEqual(JSON.parse(first.stdout));
    await expect(readdir(path.join(request.config.stateDir, "review"))).rejects.toThrow();
  });
});
