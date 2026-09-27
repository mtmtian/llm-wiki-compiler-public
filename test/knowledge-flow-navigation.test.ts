/** Observable project navigation does not depend on topic filenames or hash prefixes. */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { topicFixture } from "./knowledge-flow-topic-fixtures.js";
import { generateProjectNavigation } from "../extensions/knowledge-flow/topic-navigation.js";

it("Given readable metadata, When navigation is built, Then projects and topic titles are the entry points", async () => {
  const config = await topicFixture();
  await mkdir(path.join(config.wikiRoot, "wiki/concepts"), { recursive: true });
  await writeFile(path.join(config.wikiRoot, "wiki/concepts/opaque.md"),
    "---\ntitle: 预算与获客目标\nprojectId: repo-owner-companion\nprojectLabel: Companion\nsummary: 样例首轮素材测试的预算与目标。\n---\n正文");
  await generateProjectNavigation(config);
  const moc = await readFile(path.join(config.wikiRoot, "wiki/MOC.md"), "utf8");
  expect(moc).toContain("## Companion");
  expect(moc).toContain("[[concepts/opaque|预算与获客目标]]");
  expect(moc).not.toContain("Uncategorized");
  expect(moc).not.toContain("repo-owner-companion");
});
