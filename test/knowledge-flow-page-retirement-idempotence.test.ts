/**
 * Same-root replay regression for a reviewed whole-page retirement.
 *
 * A retirement receipt must match the exact page bytes that were reviewed. A
 * replay may not regenerate a different baseline page and then compare that
 * new body to the receipt's old hash.
 */

import { readFile, unlink, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import type { TopicMigration } from "../extensions/knowledge-flow/topic-revision-types.js";
import type { FlowConfig } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { topicFiles, topicFixture, topicRecord } from "./knowledge-flow-topic-fixtures.js";

const PAGE_ID = "concepts/publishing";
const SURVIVOR_ID = "concepts/survivor";
const BASE_PAGE = "---\ntitle: Publishing\nprojectId: companion\nknowledgeTopic: 样例素材推广\nknowledgeDecisionObject: 样例项目首轮素材测试\n---\n\n## Publishing\n";
const SURVIVOR_PAGE = "---\ntitle: Survivor\nprojectId: companion\n---\n\n## Survivor\n";
const EXTERNAL_REFERENCE = "https://github.com/example/repo/pull/9";

/** Build the minimal legacy root required by the reviewed receipt. */
async function retirementFixture(): Promise<FlowConfig> {
  const config = await topicFixture();
  await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "publishing.md"), BASE_PAGE);
  return config;
}

function pageRetirement(previous: string): TopicMigration {
  return { version: 1, basisRecordIds: [acceptedRecord().id], pages: [], retiredPages: [{
    projectId: "companion", pageId: PAGE_ID, sha256: sha256Text(previous),
    reason: "Finished coordination has no durable page content.", externalReference: EXTERNAL_REFERENCE,
  }] };
}

async function mixedRetirementFixture(targetPageId: string | null) {
  const record = topicRecord("a", ["Retired process", "Surviving decision"], { targetPageId });
  record.payload.claims[1].targetPageId = SURVIVOR_ID;
  record.payload.claims[1].topic = "Campaign budget";
  record.payload.claims[1].decisionObject = "Budget allocation";
  const initial = await retirementFixture();
  await writeFile(path.join(initial.wikiRoot, "wiki", "concepts", "survivor.md"), SURVIVOR_PAGE);
  await materializeRecords(initial, [record]);
  const previous = await topicFiles(initial);
  const sourceFiles = await topicFiles(initial, "sources");
  const config = await retirementFixture();
  await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "publishing.md"), previous.get("publishing.md")!);
  await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "survivor.md"), previous.get("survivor.md")!);
  await mkdir(path.join(config.wikiRoot, "sources"), { recursive: true });
  for (const [name, content] of sourceFiles) await writeFile(path.join(config.wikiRoot, "sources", name), content);
  return { record, config, migration: pageRetirement(previous.get("publishing.md")!), sourceFiles, previous };
}

/** Build the single accepted publication addressed by the retired page. */
function acceptedRecord(targetPageId: string | null = PAGE_ID) {
  return topicRecord("a", ["Only completed coordination remains."], { targetPageId });
}

/** Freeze one generated process page as the basis for a reviewed deletion. */
async function reviewedRetirementFixture(targetPageId: string | null = PAGE_ID) {
  const record = acceptedRecord(targetPageId); const initial = await retirementFixture();
  await materializeRecords(initial, [record]);
  const previous = await readFile(path.join(initial.wikiRoot, "wiki", "concepts", "publishing.md"), "utf8");
  const config = await retirementFixture();
  await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "publishing.md"), previous);
  return { record, config, previous, migrated: { ...config, topicMigration: pageRetirement(previous) } };
}

describe("whole-page retirement replay", () => {
  it.each([PAGE_ID, null])("does not resurrect a retired page with target %s", async targetPageId => {
    const { record, config, migrated } = await reviewedRetirementFixture(targetPageId);
    expect(await materializeRecords(migrated, [record])).toEqual({ pages: 0, conflicts: [] });
    expect(await topicFiles(config)).toEqual(new Map());
    expect(await topicFiles(config, "sources")).toEqual(new Map());
    expect(await materializeRecords(migrated, [record])).toEqual({ pages: 0, conflicts: [] });
    expect(await topicFiles(config)).toEqual(new Map());
    expect(await topicFiles(config, "sources")).toEqual(new Map());
  });

  it.each([PAGE_ID, null])("keeps shared source bytes while retiring target %s", async targetPageId => {
    const { record, config, migration, sourceFiles, previous } = await mixedRetirementFixture(targetPageId);
    const migrated = { ...config, topicMigration: migration };
    expect(await materializeRecords(migrated, [record])).toEqual({ pages: 1, conflicts: [] });
    expect(await topicFiles(config)).toEqual(new Map([["survivor.md", previous.get("survivor.md")!]]));
    expect(await topicFiles(config, "sources")).toEqual(sourceFiles);
    expect(await materializeRecords(migrated, [record])).toEqual({ pages: 1, conflicts: [] });
    expect(await topicFiles(config)).toEqual(new Map([["survivor.md", previous.get("survivor.md")!]]));
    expect(await topicFiles(config, "sources")).toEqual(sourceFiles);
  });

  it("rejects a missing page when no successful retirement receipt exists", async () => {
    const record = acceptedRecord();
    const config = await retirementFixture();
    await unlink(path.join(config.wikiRoot, "wiki", "concepts", "publishing.md"));
    const migrated = { ...config, topicMigration: pageRetirement(BASE_PAGE) };
    await expect(materializeRecords(migrated, [record])).rejects.toThrow(/basis hash mismatch/);
  });

  it("rejects a human-modified page even when a prior receipt exists", async () => {
    const { record, config, previous, migrated } = await reviewedRetirementFixture();
    await materializeRecords(migrated, [record]);
    await writeFile(path.join(config.wikiRoot, "wiki", "concepts", "publishing.md"), `${previous}\nHuman edit\n`);
    await expect(materializeRecords(migrated, [record])).rejects.toThrow(/basis hash mismatch/);
    expect((await topicFiles(config)).get("publishing.md")).toContain("Human edit");
  });
});
