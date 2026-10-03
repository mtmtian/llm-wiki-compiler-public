/** Reviewed legacy migration keeps evidence, guards user edits, and scopes extras. */
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { describe, expect, it, onTestFinished } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { applyTopicMigration } from "../extensions/knowledge-flow/topic-migration.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { readState, writeState } from "../src/utils/state.js";

const oldPage = "concepts/example-old";
const targetPage = "concepts/example-release";
const oldBody = "---\ntitle: Old\nprojectId: example\nsources:\n  - legacy.md\n---\n\n## History\n\nOld decision ^[legacy.md:1]\n";
const config: FlowConfig = { wikiRoot: "/tmp/wiki", stateDir: "/tmp/state", model: "test", maxProposals: 5, maxPendingPerProject: 10 };

function record(id: string, topic = "发布流程", object = "小批量发布"): PublicationRecord {
  const text = id === "a" ? "保留旧证据" : "新的独立事实";
  return { id: id.repeat(64), payload: { version: 2, baselineId: "b".repeat(64), machineId: "a", projectId: "example", projectLabel: "Example", createdAt: "2026-09-17T00:00:00Z", originJobHash: id.repeat(64), repoIdentity: null, basisRecordIds: [], review: { status: "accepted", model: "test" }, claims: [{ text, quote: text, evidenceId: "e1", title: topic, topic, decisionObject: object, slug: "release", targetPageId: null, kind: "decision", status: "decided", useWhen: "发布时", rationale: "保留上下文" }], evidence: [{ id: "e1", kind: "user", text, sha256: sha256Text(text), originalSha256: sha256Text(text), locator: "knowledge-evidence://a/x", observedAt: "2026-09-17T00:00:00Z" }] } };
}

function migration(basisRecordIds: string[] = [record("a").id]) {
  return { version: 1 as const, basisRecordIds, pages: [{ projectId: "example", projectLabel: "Example", pageId: targetPage, topicId: stableTopicId("example", "发布流程", "小批量发布"), title: "发布流程", topic: "发布流程", decisionObject: "小批量发布", body: "## Release\n\nOld decision ^[legacy.md:1]\n\nNew context", previousPages: [{ pageId: oldPage, sha256: sha256Text(oldBody) }] }] };
}

describe("reviewed topic migration", () => {
  it("Given source ownership and frozen old paths, When topics merge, Then future compilation sees only canonical paths", async () => {
    const root = await mkdtemp(path.join(tmpdir(), "wiki-migration-state-"));
    onTestFinished(() => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
    await writeFile(path.join(root, "wiki", `${oldPage}.md`), oldBody);
    await writeState(root, { version: 1, indexHash: "", frozenSlugs: ["example-old"], sources: {
      "legacy.md": { hash: "a".repeat(64), concepts: ["example-old"], compiledAt: "2026-09-17T00:00:00Z" },
    } });
    await materializeRecords({ ...config, wikiRoot: root, topicMigration: migration([]) }, []);
    const state = await readState(root);
    expect(state.sources["legacy.md"].concepts).toEqual(["example-release"]);
    expect(state.frozenSlugs).toEqual(["example-release"]);
    expect(state.sources["legacy.md"].hash).toBe("a".repeat(64));
  });

  it("Given the exact legacy basis, When migration applies, Then it renames, preserves citations, and removes only the old managed page", () => {
    const result = applyTopicMigration(config, migration(), new Map([[oldPage, oldBody]]), [record("a")]);
    expect(result.applied).toBe(true); expect(result.pages.has(oldPage)).toBe(false);
    const body = result.pages.get(targetPage)!; expect(body).toContain("legacy.md:1"); expect(body).toContain("New context");
    const replay = applyTopicMigration(config, migration(), result.pages, [record("a")]);
    expect(replay.applied).toBe(true); expect(replay.pages).toEqual(result.pages);
  });

  it("Given an extra unrelated legacy record, Then migration remains applicable without dropping it from the input", () => {
    const result = applyTopicMigration(config, migration(), new Map([[oldPage, oldBody]]), [record("a"), record("b", "其他主题", "其他对象")]);
    expect(result.applied).toBe(true); expect(result.conflicts).toEqual([]);
  });

  it("Given a ledger record for the migrated object, Then migration does not hold it as an extra legacy record", () => {
    const ledger = record("b"); ledger.payload.version = 3;
    const result = applyTopicMigration(config, migration(), new Map([[oldPage, oldBody]]), [record("a"), ledger]);
    expect(result.applied).toBe(true); expect(result.conflicts).toEqual([]);
  });

  it("Given an extra record for the migrated object or a missing basis, Then migration holds or fails closed", () => {
    const same = applyTopicMigration(config, migration(), new Map([[oldPage, oldBody]]), [record("a"), record("b")]);
    expect(same.applied).toBe(true); expect(same.conflicts[0].reason).toMatch(/extra legacy/);
    expect(same.pages.has(targetPage)).toBe(true); expect(same.pages.has(oldPage)).toBe(false);
    expect(() => applyTopicMigration(config, migration([record("c").id]), new Map([[oldPage, oldBody]]), [record("a")])).toThrow(/basis records/);
  });

  it("Given an existing target omitted from previousPages, Then migration refuses to overwrite it", () => {
    const existing = new Map([[oldPage, oldBody], [targetPage, oldBody.replace("Old", "Other")]]);
    expect(() => applyTopicMigration(config, migration(), existing, [record("a")])).toThrow(/previousPages/);
  });

  it("Given a changed citation span, Then migration refuses to retain only the source filename", () => {
    const changed = migration(); changed.pages[0].body = "## Release\n\nOld decision ^[legacy.md:2]\n\nNew context";
    expect(() => applyTopicMigration(config, changed, new Map([[oldPage, oldBody]]), [record("a")])).toThrow(/citation/);
  });

  it("Given a curated canonical page, Then a merge preserves its custom metadata regardless of previous-page order", () => {
    const canonical = oldBody.replace("title: Old", "title: Curated\ncuratedBy: human");
    const input = migration();
    input.pages[0].previousPages.push({ pageId: targetPage, sha256: sha256Text(canonical) });
    const result = applyTopicMigration(config, input, new Map([[oldPage, oldBody], [targetPage, canonical]]), [record("a")]);
    expect(result.pages.get(targetPage)).toContain("curatedBy: human");
    expect(result.pages.get(targetPage)).toContain("New context");
  });

  it.each([false, true])("Given an exact basis, When canonical target is retained=%s, Then migration survives extra records", async (canonical) => {
    const root = await mkdtemp(path.join(tmpdir(), "wiki-migration-view-"));
    onTestFinished(() => rm(root, { recursive: true, force: true }));
    await mkdir(path.join(root, "wiki/concepts"), { recursive: true });
    const basis = record("a"); basis.payload.claims[0].targetPageId = oldPage;
    const seeded = oldBody.replace("---\n", `---\nknowledgeTopic: 发布流程\nknowledgeDecisionObject: 小批量发布\nknowledgePublicationRefs:\n  - ${basis.id}:0\n`);
    await writeFile(path.join(root, "wiki", `${oldPage}.md`), seeded);
    const migrationConfig: FlowConfig = { ...config, wikiRoot: root, stateDir: path.join(root, "state"), topicMigration: migration() };
    const destination = canonical ? oldPage : targetPage;
    migrationConfig.topicMigration!.pages[0].pageId = destination;
    migrationConfig.topicMigration!.pages[0].body += "\n\nSee [[concepts/example-old]]";
    migrationConfig.topicMigration!.pages[0].previousPages[0].sha256 = sha256Text(seeded);
    const extra = canonical ? record("b") : record("b", "其他主题", "其他对象");
    const result = await materializeRecords(migrationConfig, [basis, extra]);
    expect(result.conflicts.length).toBe(canonical ? 1 : 0);
    const target = await readFile(path.join(root, "wiki", `${destination}.md`), "utf8");
    expect(target).toContain("New context"); expect(target).toContain(`[[${destination}]]`);
    expect(target).not.toContain("新的独立事实");
    expect((await readdir(path.join(root, "wiki/concepts"))).some(name => name.includes("example-old"))).toBe(canonical);
  });
});
