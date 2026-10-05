/**
 * Behaviour checks for the private knowledge-flow adapter.
 *
 * Providers are injected as inert fakes; the success case exercises the real
 * compiler candidate and locked approval path against a temporary wiki.
 */

import { mkdtemp, readFile, rm, mkdir, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { describe, expect, it, afterEach } from "vitest";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { resolvePendingReview } from "../extensions/knowledge-flow/resolve.js";
import type { FlowClaim, FlowConfig, FlowDependencies, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { LLMProvider } from "../src/utils/provider.js";

const roots: string[] = [];
const fakeProvider = {} as LLMProvider;

afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))); });

function claim(targetPageId: string | null = null): FlowClaim {
  return { text: "保留项目决策", evidenceId: "e1", quote: "保留项目决策", title: "项目决策", topic: "项目", slug: "project-decision", targetPageId, kind: "decision", status: "decided", useWhen: "后续项目实施", rationale: "会改变后续执行" };
}

async function fixture(): Promise<{ root: string; config: FlowConfig; job: FlowJob }> {
  const root = await mkdtemp(path.join(tmpdir(), "knowledge-flow-"));
  roots.push(root);
  await mkdir(path.join(root, "wiki", "concepts"), { recursive: true });
  const text = "保留项目决策";
  const job: FlowJob = { id: "job-1", projectId: "work/project", projectLabel: "Project", sessionId: "s1", turnId: "t1", cwd: root, createdAt: "2026-09-14T12:00:00.000Z", prompt: "完成项目决策", lastAssistant: "已完成", evidence: [{ id: "e1", kind: "user", text, locator: "turn:t1", sha256: createHash("sha256").update(text).digest("hex"), observedAt: "2026-09-14T12:00:00.000Z" }], allowedPageIds: [] };
  return { root, job, config: { wikiRoot: root, stateDir: path.join(root, "state"), model: "gpt-6-luna", maxProposals: 5, maxPendingPerProject: 10, provider: fakeProvider, reviewer: fakeProvider } };
}

async function seedPage(root: string, slug: string, body = "旧页面内容足够长用于验证更新"):
  Promise<string> {
  const file = path.join(root, "wiki", "concepts", `${slug}.md`);
  await writeFile(file, `---\ntitle: ${slug}\nsummary: old\n---\n\n${body}\n`);
  return file;
}

function deps(extracted: FlowClaim[], decision: "accept" | "reject" | "needs_review"): FlowDependencies {
  return { extract: async () => extracted, review: async () => extracted.map((_, index) => ({ index, decision, reason: "checked", conflictingPageIds: [] })) };
}

describe("knowledge flow", () => {
  it("rejects an out-of-scope page id without calling providers", async () => {
    const { job, config } = await fixture();
    job.allowedPageIds = ["../other/secret"];
    const result = await processJob(job, config, deps([], "reject"));
    expect(result.status).toBe("error");
  });

  it("returns empty when no evidence exists", async () => {
    const { job, config } = await fixture();
    job.evidence = [];
    const result = await processJob(job, config, deps([claim()], "accept"));
    expect(result.status).toBe("empty");
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
  });

  it("rejects a reviewer rejection without creating a review item", async () => {
    const { job, config } = await fixture();
    const result = await processJob(job, config, deps([claim()], "reject"));
    expect(result).toMatchObject({ status: "empty", reviewCount: 0 });
    await expect(readdir(path.join(config.stateDir, "review"))).rejects.toThrow();
  });

  it("refuses more claims than the configured hard limit", async () => {
    const { job, config } = await fixture();
    config.maxProposals = 2;
    const result = await processJob(job, config, deps([claim(), { ...claim(), slug: "second" }, { ...claim(), slug: "third" }], "accept"));
    expect(result.status).toBe("error");
  });

  it("publishes five distinct accepted claims and preserves them on retry", async () => {
    const { job, config } = await fixture();
    const proposals = Array.from({ length: 5 }, (_, index) => ({ ...claim(), slug: `decision-${index}`, text: `项目决策 ${index}` }));
    const result = await processJob(job, config, deps(proposals, "accept"));
    expect(result.status).toBe("published");
    expect(result.publishedPageIds).toHaveLength(5);
    expect(new Set(result.publishedPageIds).size).toBe(5);
    expect(await processJob(job, config, deps(proposals, "accept"))).toEqual(result);
    expect(await readdir(path.join(config.wikiRoot, "wiki", "concepts"))).toHaveLength(5);
  });

  it("refuses six claims even when configuration requests more", async () => {
    const { job, config } = await fixture();
    config.maxProposals = 20;
    const result = await processJob(job, config, deps(Array.from({ length: 6 }, () => claim()), "accept"));
    expect(result).toMatchObject({ status: "error", error: "extraction exceeded proposal limit", publishedPageIds: [] });
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
  });

  it("does not publish two accepted claims to the same target", async () => {
    const { job, config } = await fixture();
    const result = await processJob(job, config, deps([claim(), { ...claim(), text: "另一条结论" }], "accept"));
    expect(result.status).toBe("needs_review");
    expect(result.publishedPageIds).toEqual([]);
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
  });

  it("blocks a changed existing page during locked publish", async () => {
    const { job, config } = await fixture();
    job.allowedPageIds = ["concepts/existing"];
    await seedPage(config.wikiRoot, "existing");
    const changed = async () => {
      await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "existing.md"), "---\ntitle: existing\nsummary: changed\n---\n\n外部变化\n");
      return [{ index: 0, decision: "accept" as const, reason: "checked", conflictingPageIds: [] }];
    };
    const result = await processJob(job, config, { extract: async () => [claim("concepts/existing")], review: changed as any });
    expect(result.status).toBe("needs_review");
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
  });

  it("supports Unicode existing page ids", async () => {
    const { job, config } = await fixture();
    job.allowedPageIds = ["concepts/中文页"];
    await seedPage(config.wikiRoot, "中文页");
    const result = await processJob(job, config, deps([claim("concepts/中文页")], "accept"));
    expect(result.status).toBe("published");
    expect(result.publishedPageIds).toEqual(["concepts/中文页"]);
  });

  it("routes reviewer conflicts to state without writing wiki or sources", async () => {
    const { job, config } = await fixture();
    const result = await processJob(job, config, deps([claim()], "needs_review"));
    expect(result.status).toBe("needs_review");
    expect(result.reviewFile).toBeDefined();
    const review = JSON.parse(await readFile(result.reviewFile!, "utf8")) as { evidence: Array<{ quote: string; locator: string }> };
    expect(review.evidence[0]).toMatchObject({ quote: "保留项目决策", locator: "turn:t1" });
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
    await expect(readdir(path.join(config.wikiRoot, "wiki", "concepts"))).resolves.toEqual([]);
    await expect(resolvePendingReview(config.stateDir, job.id, "dismiss")).resolves.toBe(true);
  });

  it("never exposes an accept operation for pending reviews", async () => {
    const { config } = await fixture();
    await expect(resolvePendingReview(config.stateDir, "job-1", "accept" as any)).rejects.toThrow("only support reject or dismiss");
  });

  it("publishes an accepted candidate and is idempotent on retry", async () => {
    const { job, config } = await fixture();
    const result = await processJob(job, config, deps([claim()], "accept"));
    expect(result.status).toBe("published");
    expect(result.publishedPageIds[0]).toMatch(/^concepts\/work-project-[a-f0-9]{8}-project-decision$/);
    const page = await readFile(path.join(config.wikiRoot, "wiki", `${result.publishedPageIds[0]}.md`), "utf8");
    expect(page).toContain("projectId: work/project");
    expect(page).toContain("保留项目决策");
    const retry = await processJob(job, config, deps([claim()], "accept"));
    expect(retry).toEqual(result);
    expect((await readdir(path.join(config.wikiRoot, "sources"))).length).toBe(1);
  });

  it("fails closed when a mapped page is missing", async () => {
    const { job, config } = await fixture();
    job.allowedPageIds = ["concepts/not-downloaded"];
    const result = await processJob(job, config, deps([claim()], "accept"));
    expect(result.status).toBe("error");
    await expect(readdir(path.join(config.wikiRoot, "sources"))).rejects.toThrow();
  });

  it("resumes an existing-page publication without appending twice or rerunning models", async () => {
    const { job, config } = await fixture();
    const file = await seedPage(config.wikiRoot, "中文页");
    job.allowedPageIds = ["concepts/中文页"];
    await processJob(job, config, deps([claim("concepts/中文页")], "accept"));
    const body = await readFile(file, "utf8");
    await rm(path.join(config.stateDir, "audit", "job-1.json"));
    const attemptFile = path.join(config.stateDir, "attempts", "job-1.json");
    const attempt = JSON.parse(await readFile(attemptFile, "utf8"));
    attempt.status = "publishing";
    delete attempt.result;
    await writeFile(attemptFile, JSON.stringify(attempt));
    const forbidden = async () => { throw new Error("model reran during recovery"); };
    const result = await processJob(job, config, { extract: forbidden, review: forbidden });
    expect(result).toMatchObject({ status: "published", publishedPageIds: job.allowedPageIds, reviewCount: 0 });
    expect(await readFile(file, "utf8")).toBe(body);
  });

  it("reconstructs blocked review items when a completed attempt lost its final audit", async () => {
    const { job, config } = await fixture();
    await processJob(job, config, deps([claim(), { ...claim(), text: "另一条结论" }], "accept"));
    await rm(path.join(config.stateDir, "audit", "job-1.json"));
    await rm(path.join(config.stateDir, "review", "job-1.json"));
    const result = await processJob(job, config, deps([], "reject"));
    expect(result).toMatchObject({ status: "needs_review", reviewCount: 2 });
    expect(JSON.parse(await readFile(result.reviewFile!, "utf8")).claims).toHaveLength(2);
  });

  it("keeps an uncertain item when recovering a mixed publication", async () => {
    const { job, config } = await fixture();
    const claims = [claim(), { ...claim(), slug: "uncertain", text: "未确定结论", status: "uncertain" as const }];
    const first = await processJob(job, config, deps(claims, "accept"));
    expect(first.reviewCount).toBe(1);
    await rm(path.join(config.stateDir, "audit", "job-1.json"));
    const recovered = await processJob(job, config, deps([], "reject"));
    expect(recovered.reviewCount).toBe(1);
    expect(recovered.reviewFile).toBe(first.reviewFile);
  });
});
