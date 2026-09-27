/** Select whole decision sections, preserving local qualifications and their citations. */
import { createHash } from "node:crypto";
import type { ViewerPage } from "../viewer/types.js";
import type { SemanticChunkHit } from "./retrieval.js";
import { rankTaskSections } from "./task-ranking.js";
import { sectionTemporalStatus, type SectionTemporalStatus } from "./task-temporal.js";

export interface DecisionSection {
  page: ViewerPage;
  heading: string;
  text: string;
  qualifications?: string;
  level: number;
  score: number;
  temporalStatus: SectionTemporalStatus;
}

/** Hash the entire page, including qualifications outside the previously selected excerpt. */
export function taskPageRevision(page: ViewerPage): string {
  return createHash("sha256").update(JSON.stringify(page.frontmatter)).update(page.body).digest("hex");
}

/** Keep heading sections intact so qualifications stay with the cited decision. */
function pageSections(page: ViewerPage): DecisionSection[] {
  const sections: DecisionSection[] = [];
  const headings: string[] = [];
  let lines: string[] = [];
  let fence = false;
  const flush = () => {
    const text = lines.join("\n").trim();
    const heading = headings.filter(Boolean).join(" / ") || page.title;
    if (text) sections.push({ page, heading, text, score: 0, level: headings.length,
      temporalStatus: sectionTemporalStatus(heading, page.frontmatter.status) });
    lines = [];
  };
  for (const line of page.body.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fence = !fence;
    const heading = !fence && /^(#{1,6})\s+(.+)$/.exec(line);
    if (!heading) { lines.push(line); continue; }
    flush();
    headings.length = heading[1].length;
    headings[heading[1].length - 1] = heading[2];
  }
  flush();
  return sections;
}

/** Semantic hits guide selection of current body text; all ranking stays within verified scope. */
export function rankDecisionSections(pages: ViewerPage[], prompt: string, hits: SemanticChunkHit[],
  currentProjectId?: string): DecisionSection[] {
  return rankTaskSections(pages.flatMap(page => withQualifications(pageSections(page))), prompt, hits, currentProjectId);
}

/** Page-level applicability travels with each decision, even without query-word overlap. */
function withQualifications(sections: DecisionSection[]): DecisionSection[] {
  const scope = sections.filter((section, index) => (index === 0 && section.level < 2) ||
    /^(适用范围|适用条件|目标与状态|适用边界.*|scope|applicability)$/i.test(section.heading.split(" / ").at(-1) ?? ""));
  const decisions = sections.filter(section => !scope.includes(section));
  if (!decisions.length) return sections;
  const qualifications = scope.map(item => item.text).join("\n\n");
  return decisions.map(section => ({ ...section, qualifications }));
}
