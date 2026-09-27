/** Create real temporary Markdown pages, source files, and source state for evaluation. */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { DECISION_REUSE_PAGES, type SyntheticPage } from "./decision-reuse-corpus.js";
import { sha256Hex, writeSourceFile, writeSourceState } from "./state-json.js";
import { writePage } from "./write-page.js";

export interface DecisionReuseWiki {
  root: string;
  sourceState: Record<string, { hash: string; concepts: string[] }>;
}

/** Seed synthetic knowledge and independently cited sources on disk. */
export async function seedDecisionReuseWiki(root: string): Promise<DecisionReuseWiki> {
  const sourceState: DecisionReuseWiki["sourceState"] = {};
  const conceptDirectory = path.join(root, "wiki", "concepts");
  await mkdir(conceptDirectory, { recursive: true });
  for (const page of DECISION_REUSE_PAGES) {
    await seedPage(root, conceptDirectory, page, sourceState);
  }
  await writeSourceState(root, sourceState);
  return { root, sourceState };
}

/** Add later evidence without deleting or rewriting the historical source record. */
export async function addDecisionReuseFollowUp(wiki: DecisionReuseWiki): Promise<void> {
  const source = "2026年9月新增材料确认当前继续使用 Markdown 主题页作为可审阅投影；该补充没有改变 2024 年历史决定及其形成原因。";
  const sourceFile = "timeline-2026.md";
  await writeSourceFile(wiki.root, sourceFile, `${source}\n`);
  wiki.sourceState[sourceFile] = { hash: sha256Hex(`${source}\n`), concepts: ["decision-timeline"] };
  const pagePath = path.join(wiki.root, "wiki", "concepts", "decision-timeline.md");
  const currentSection = `\n## 当前决定（2026）\n${source} ^[${sourceFile}:1]\n`;
  await writeFile(pagePath, `${await readFile(pagePath, "utf8")}${currentSection}`, "utf8");
  await writeSourceState(wiki.root, wiki.sourceState);
}

async function seedPage(root: string, directory: string, page: SyntheticPage,
  sourceState: DecisionReuseWiki["sourceState"]): Promise<void> {
  const body: string[] = [];
  for (const section of page.sections) {
    const sourceText = section.text;
    await writeSourceFile(root, section.source, `${sourceText}\n`);
    sourceState[section.source] = { hash: sha256Hex(`${sourceText}\n`), concepts: [page.slug] };
    body.push(`## ${section.heading}`, `${section.text} ^[${section.source}:1]`);
  }
  await writePage(directory, page.slug, page.fields, body.join("\n\n"));
}
