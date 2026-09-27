/** Prior-page evidence comes from markdown bodies, never truncated YAML summaries. */
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { tmpdir } from "node:os";
import { describe, expect, it, onTestFinished } from "vitest";
import { priorSourceContext } from "../extensions/knowledge-flow/consolidation-sources.js";
import type { PlannedPage } from "../extensions/knowledge-flow/consolidation-plan.js";
import { buildFrontmatter } from "../src/utils/markdown.js";

async function sourceRoot(): Promise<string> {
  const root = await mkdtemp(path.join(tmpdir(), "wiki-prior-sources-"));
  await mkdir(path.join(root, "sources"));
  await writeFile(path.join(root, "sources/legacy.md"), "evidence\n");
  return root;
}

function plannedPage(original: string): PlannedPage {
  return { pageId: "concepts/example", topicId: "topic", title: "Example", topic: "Topic",
    decisionObject: "Decision", basisHash: null, original };
}

describe("prior citation source context", () => {
  it("Given a truncated frontmatter citation and a valid body citation, When sources load, Then only the body source is read", async () => {
    const root = await sourceRoot(); onTestFinished(() => rm(root, { recursive: true, force: true }));
    const original = `${buildFrontmatter({ summary: "Prior context ^[../../outside.md:" })}\n\nEvidence ^[legacy.md:1]`;
    const result = await priorSourceContext(root, [plannedPage(original)]);
    expect(Object.keys(result)).toEqual(["legacy.md"]); expect(result["legacy.md"]).toBe("evidence\n");
  });

  it.each(["../../outside.md:1", "../../outside.md:1\ncontinued"])("rejects an out-of-root body citation, including damaged cross-line markers (%#)", async marker => {
    const root = await sourceRoot(); onTestFinished(() => rm(root, { recursive: true, force: true }));
    const original = `${buildFrontmatter({ summary: "Safe summary" })}\n\nEvidence ^[${marker}]`;
    await expect(priorSourceContext(root, [plannedPage(original)])).rejects.toThrow(/path escapes project root/);
  });
});
