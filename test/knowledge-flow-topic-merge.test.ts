/**
 * Reviewed merges of revision-layer topic pages. Every replica generation replays all records from the
 * baseline, so a merge must survive replay: the absorbed records rebuild the previous pages, the merge
 * replaces them with the reviewed body, and later records apply to the merged page. A removed page must
 * never be recreated, and any drift from the reviewed bytes or a dropped citation fails closed.
 */
import { writeFile } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { sha256Text } from "../src/connectors/hash.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { TopicMerge } from "../extensions/knowledge-flow/topic-revision-types.js";
import { makeKnowledgeFlowConfig, materializeKnowledgeFlow } from "./knowledge-flow-test-fixtures.js";

const ALPHA = "concepts/alpha-topic-aaaa1111";
const BETA = "concepts/beta-topic-bbbb2222";
const GAMMA = "concepts/gamma-topic-cccc3333";

interface RecordSpec { name: string; pageId: string; text: string; basisHash: string | null; extra?: string; basis?: string[]; semantic?: boolean; at?: string }

/** One accepted revision record with a single decision claim citing its own evidence. */
function record(spec: RecordSpec): PublicationRecord {
  const id = sha256Text(spec.name); const topic = `${spec.pageId} 主题`; const decisionObject = `${spec.pageId} 对象`;
  const revision = { pageId: spec.pageId, topicId: stableTopicId("project-a", topic, decisionObject), title: `${spec.name} 标题`, topic,
    decisionObject, basisHash: spec.basisHash, body: `## 结论\n\n${spec.text} {{claim:0}}${spec.extra ?? ""}`, claimIndexes: [0],
    ...(spec.semantic ? { topicScope: "semantic" as const } : {}) };
  return { id, payload: { version: 2, baselineId: "b".repeat(64), machineId: "a", projectId: "project-a", projectLabel: "Project A",
    createdAt: spec.at ?? "2026-09-25T00:00:00Z", originJobHash: id, repoIdentity: null, basisRecordIds: spec.basis ?? [],
    review: { status: "accepted", model: "test" },
    claims: [{ text: spec.text, quote: spec.text, evidenceId: "e1", title: `${spec.name} 标题`, topic, decisionObject, slug: spec.name,
      targetPageId: spec.pageId, kind: "decision", status: "decided", useWhen: "整理样例主题时", rationale: "样例依据" }],
    evidence: [{ id: "e1", kind: "user", text: spec.text, sha256: sha256Text(spec.text), originalSha256: sha256Text(spec.text),
      locator: `knowledge-evidence://project-a/${spec.name}`, observedAt: "2026-09-25T00:00:00Z" }],
    topicRevisions: [revision] } };
}

const alpha = record({ name: "alpha", pageId: ALPHA, text: "样例甲的结论", basisHash: null });
const beta = record({ name: "beta", pageId: BETA, text: "样例乙的结论", basisHash: null, at: "2026-09-26T00:00:00Z" });
const gamma = record({ name: "gamma", pageId: GAMMA, text: "样例丙的结论", basisHash: null, extra: "\n\n相关：[[beta-topic-bbbb2222]]", at: "2026-09-27T00:00:00Z" });

async function materialize(records: PublicationRecord[], merges?: TopicMerge[]) {
  const config = { ...await makeKnowledgeFlowConfig("wiki-topic-merge-"), ...(merges ? { topicMerges: merges } : {}) };
  return materializeKnowledgeFlow(config, records);
}

/** Freeze exactly the reviewed page bodies and hashes for either merge scenario. */
function mergeFromPages(pages: Array<[string, string]>, metadata: Omit<TopicMerge, "body" | "previousPages" | "mergedAt">): TopicMerge {
  return { ...metadata, body: pages.map(([, page]) => parseFrontmatter(page).body.trim()).join("\n\n"),
    previousPages: pages.map(([pageId, page]) => ({ pageId, sha256: sha256Text(page) })),
    mergedAt: "2026-10-04T00:00:00Z" };
}

/** Review the pages as they are now and merge beta into alpha, keeping every citation of both. */
async function reviewedMerge(overrides: Partial<TopicMerge> = {}): Promise<TopicMerge> {
  const { read } = await materialize([alpha, beta, gamma]);
  const [alphaPage, betaPage] = [(await read(ALPHA))!, (await read(BETA))!];
  return { ...mergeFromPages([[ALPHA, alphaPage], [BETA, betaPage]], {
    pageId: ALPHA, title: "甲乙合并", topic: `${ALPHA} 主题`, decisionObject: `${ALPHA} 对象`,
    absorbedRecordIds: [alpha.id, beta.id], reason: "同属一个样例对象" }), ...overrides };
}

describe("reviewed merges of revision-layer pages", () => {
  it("Given two pages built by revisions, When merged, Then the survivor holds both and the other page is never rebuilt", async () => {
    const merge = await reviewedMerge();
    const { result, read } = await materialize([alpha, beta, gamma], [merge]);
    const merged = (await read(ALPHA))!; const meta = parseFrontmatter(merged).meta;
    expect(result.conflicts).toEqual([]);
    expect(await read(BETA)).toBeNull();
    expect(merged).toContain("样例甲的结论"); expect(merged).toContain("样例乙的结论");
    expect(meta).toMatchObject({ title: "甲乙合并", topicScope: "semantic", sourceProjectIds: ["project-a"], updatedAt: "2026-10-04T00:00:00Z" });
    expect(meta.knowledgePublicationRefs).toEqual(expect.arrayContaining([`${alpha.id}:0`, `${beta.id}:0`]));
    expect(meta.knowledgeMergedPages).toEqual([ALPHA, BETA]);
    expect(await read(GAMMA)).toContain("[[alpha-topic-aaaa1111]]");
  });

  it("Given records replayed in another order, Then the merged generation is identical", async () => {
    const merge = await reviewedMerge();
    const first = await materialize([alpha, beta, gamma], [merge]);
    const second = await materialize([gamma, beta, alpha], [merge]);
    expect(await second.read(ALPHA)).toBe(await first.read(ALPHA));
    expect(await second.read(GAMMA)).toBe(await first.read(GAMMA));
  });

  it("Given a later semantic revision of the merged page, Then it applies on top of the merge", async () => {
    const merge = await reviewedMerge();
    const mergedBytes = (await (await materialize([alpha, beta, gamma], [merge])).read(ALPHA))!;
    const later = record({ name: "later", pageId: ALPHA, text: "合并后的新结论", basisHash: sha256Text(mergedBytes), semantic: true,
      extra: `\n\n${parseFrontmatter(mergedBytes).body.trim()}`, basis: [alpha.id, beta.id], at: "2026-10-05T00:00:00Z" });
    const { result, read } = await materialize([alpha, beta, gamma, later], [merge]);
    expect(result.conflicts).toEqual([]);
    expect(await read(ALPHA)).toContain("合并后的新结论");
    expect(await read(BETA)).toBeNull();
  });

  it("Given later records that still revise or recreate the removed page, Then they are held and the page stays removed", async () => {
    const merge = await reviewedMerge();
    const stale = record({ name: "stale", pageId: BETA, text: "仍写到旧页的结论", basisHash: merge.previousPages[1].sha256,
      basis: [beta.id], at: "2026-10-05T00:00:00Z" });
    const recreated = record({ name: "recreated", pageId: BETA, text: "重新创建旧页的结论", basisHash: null, at: "2026-10-06T00:00:00Z" });
    const { result, read } = await materialize([alpha, beta, gamma, stale, recreated], [merge]);
    expect(result.conflicts.map(conflict => conflict.reason)).toEqual(Array(2).fill(`topic page was merged into ${ALPHA}; revise the merged page instead`));
    expect(await read(BETA)).toBeNull();
  });

  it("Given previous bytes that differ from the review or a dropped citation, Then the generation fails closed", async () => {
    const drifted = await reviewedMerge();
    drifted.previousPages[1] = { pageId: BETA, sha256: "0".repeat(64) };
    await expect(materialize([alpha, beta, gamma], [drifted])).rejects.toThrow(/reviewed bytes/);
    const dropped = await reviewedMerge();
    dropped.body = dropped.body.replace(/样例乙的结论 \^\[[^\]]+\]/, "样例乙的结论");
    await expect(materialize([alpha, beta, gamma], [dropped])).rejects.toThrow(/citation/i);
  });
});

const LEGACY = "concepts/sample-legacy";
const MIGRATED = "concepts/sample-release";
const legacyBody = "---\ntitle: 旧页\nprojectId: project-a\nsources:\n  - legacy.md\n---\n\n## 历史\n\n旧的样例决定 ^[legacy.md:1]\n";
/** The one-time legacy migration renders MIGRATED from a baseline page before revisions replay and merges apply. */
const migration = { version: 1 as const, basisRecordIds: [], pages: [{ projectId: "project-a", projectLabel: "Project A", pageId: MIGRATED,
  topicId: stableTopicId("project-a", "样例发布", "小批量发布"), title: "样例发布", topic: "样例发布", decisionObject: "小批量发布",
  body: "## 发布\n\n旧的样例决定 ^[legacy.md:1]", previousPages: [{ pageId: LEGACY, sha256: sha256Text(legacyBody) }] }] };

async function materializeMigrated(merges?: TopicMerge[]) {
  const config = { ...await makeKnowledgeFlowConfig("wiki-merge-migration-"), topicMigration: migration, ...(merges ? { topicMerges: merges } : {}) };
  await writeFile(path.join(config.wikiRoot, "wiki", `${LEGACY}.md`), legacyBody);
  return materializeKnowledgeFlow(config, [alpha]);
}

/** Review the migration page and the revision page as they are now, merged into `survivor`. */
async function migratedMerge(survivor: string): Promise<TopicMerge> {
  const { read } = await materializeMigrated();
  const [alphaPage, migratedPage] = [(await read(ALPHA))!, (await read(MIGRATED))!];
  return mergeFromPages([[ALPHA, alphaPage], [MIGRATED, migratedPage]], {
    pageId: survivor, title: "甲与发布合并", topic: "样例发布", decisionObject: "小批量发布",
    absorbedRecordIds: [alpha.id], reason: "同属一个样例工作流" });
}

describe("reviewed merges that include a page of the legacy migration", () => {
  it("Given a migration page merged into a revision page, When replayed, Then only the survivor remains and holds both", async () => {
    const merge = await migratedMerge(ALPHA);
    const { result, read } = await materializeMigrated([merge]);
    expect(result.conflicts).toEqual([]);
    expect(await read(MIGRATED)).toBeNull(); expect(await read(LEGACY)).toBeNull();
    expect(await read(ALPHA)).toContain("旧的样例决定 ^[legacy.md:1]");
    expect(await read(ALPHA)).toContain("样例甲的结论");
  });

  it("Given a revision page merged into the migration page, When replayed twice, Then the migration page is the stable survivor", async () => {
    const merge = await migratedMerge(MIGRATED);
    const first = await materializeMigrated([merge]);
    const second = await materializeMigrated([merge]);
    expect(first.result.conflicts).toEqual([]);
    expect(await first.read(ALPHA)).toBeNull();
    expect(parseFrontmatter((await first.read(MIGRATED))!).meta.knowledgeMergedPages).toEqual([ALPHA, MIGRATED]);
    expect(await second.read(MIGRATED)).toBe(await first.read(MIGRATED));
  });
});
