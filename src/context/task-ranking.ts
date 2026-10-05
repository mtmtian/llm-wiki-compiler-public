/** Gate task relevance before rank fusion: the best unrelated neighbour is still unrelated evidence. */
import type { DecisionSection } from "./task-sections.js";
import type { SemanticChunkHit } from "./retrieval.js";
import { taskTemporalIntent } from "./task-temporal.js";
import { sourceProjectIds } from "../utils/topic-scope.js";

const QUESTION_WORDS = new Set(("这个 这次 当前 现在 之前 此前 以前 是否 怎么 如何 怎样 为什么 哪些 哪个 哪种 哪里 什么时候 多少 什么 还是 应该 需要 可以 一个 一下 我们 你们 仍 要 做 的 了 吗 呢 是 在 和 与 或 及 "
  + "the a an is are was were how why what which where when should can please this that do does did will would with for of to in on at and or").split(" "));
const segmenter = new Intl.Segmenter("zh", { granularity: "word" });
const MIN_QUERY_COVERAGE = 0.3;
const MIN_RELATIVE_LEXICAL_SCORE = 0.5;
const SEMANTIC_ONLY_MINIMUM = 0.8;
const SEMANTIC_SUPPORT_MINIMUM = 0.5;
/**
 * Another project's decision needs several shared terms plus semantic agreement, not one coincidental word.
 * Calibrated on labelled real prompts with local nomic-embed-text scores; recalibrate if the embedding model changes.
 * Without embeddings, broad lexical coverage is the fallback evidence.
 */
const MIN_CROSS_PROJECT_TERMS = 2;
/** Cross-project topical lookup needs more than one surviving word; project-scoped lookups keep short topic queries. */
const MIN_QUERY_TERMS = 2;
const CROSS_PROJECT_SEMANTIC_MINIMUM = 0.6;
const MIN_CROSS_PROJECT_COVERAGE = 0.5;
const RRF_OFFSET = 60;
const HEADING_WEIGHT = 3;
interface Candidate { section: DecisionSection; lexical: number; semantic: number; coverage: number; matches: number; headingMatch: boolean }

/**
 * Scope and citation validation remain outside ranking; qualifications never establish relevance by themselves.
 * `currentProjectId` is set only for cross-project (semantic) scope, where other projects' pages face a stricter gate.
 */
export function rankTaskSections(sections: DecisionSection[], prompt: string, hits: SemanticChunkHit[],
  currentProjectId?: string): DecisionSection[] {
  const terms = queryTerms(prompt);
  if (!terms.length || (currentProjectId && terms.length < MIN_QUERY_TERMS)) return [];
  const intent = taskTemporalIntent(prompt);
  const pool = sections.filter(section => intent !== "current" || section.temporalStatus !== "historical");
  const weights = termWeights(pool, terms);
  const candidates = pool.map(section => scoreSection(section, weights, hits));
  const eligible = candidates.filter(candidate => admitted(candidate, terms.length, currentProjectId, hits.length > 0));
  const lexicalCandidates = eligible.filter(candidate => lexicalMatch(candidate, terms.length));
  const bestLexical = Math.max(0, ...lexicalCandidates.map(candidate => candidate.lexical));
  const lexical = lexicalCandidates.filter(candidate => candidate.lexical >= bestLexical * MIN_RELATIVE_LEXICAL_SCORE)
    .sort((a, b) => b.lexical - a.lexical);
  const semantic = eligible.filter(candidate => candidate.semantic >= SEMANTIC_SUPPORT_MINIMUM)
    .sort((a, b) => b.semantic - a.semantic);
  const fused = new Map<DecisionSection, number>();
  for (const arm of [lexical, semantic]) arm.forEach((candidate, rank) => {
    fused.set(candidate.section, (fused.get(candidate.section) ?? 0) + 1 / (RRF_OFFSET + rank + 1));
  });
  return [...fused].map(([section, score]) => ({ ...section, score })).sort((a, b) =>
    b.score - a.score || historyPreference(b, intent) - historyPreference(a, intent)
    || a.page.id.localeCompare(b.page.id) || a.heading.localeCompare(b.heading));
}

/** Do not let single Han characters or English substrings such as CI inside precision create false hits. */
function queryTerms(text: string): string[] {
  return [...new Set([...segmenter.segment(clean(text))].filter(part => part.isWordLike
    && !QUESTION_WORDS.has(part.segment) && !/^\p{Script=Han}$/u.test(part.segment)).map(part => part.segment))];
}

/** Source filenames are provenance rather than relevance terms. */
function clean(text: string): string { return text.normalize("NFKC").toLowerCase().replace(/\^\[[^\]]*\]/g, ""); }

/** Match CJK words in prose and Latin identifiers only at token boundaries. */
function matches(text: string, term: string): boolean {
  if (/\p{Script=Han}/u.test(term)) return text.includes(term);
  return new RegExp(`(^|[^a-z0-9_])${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^a-z0-9_])`, "u").test(text);
}

/** Topic metadata supplies context even when a page omits its H1 heading. */
function sectionText(section: DecisionSection): string {
  return clean(`${section.page.title}\n${section.heading}\n${section.text}\n${section.page.frontmatter.knowledgeTopic ?? ""}\n${section.page.frontmatter.knowledgeDecisionObject ?? ""}`);
}

/** Unmatched query terms stay in the denominator: relative rank alone cannot justify injection. */
function termWeights(sections: DecisionSection[], terms: string[]): Map<string, number> {
  const texts = sections.map(sectionText);
  return new Map(terms.map(term => [term, 1 + Math.log((texts.length + 1) / (1 + texts.filter(text => matches(text, term)).length))]));
}

/** Keep term coverage separate from heading boosts so generic headings cannot force inclusion. */
function scoreSection(section: DecisionSection, weights: Map<string, number>, hits: SemanticChunkHit[]): Candidate {
  const text = sectionText(section);
  const heading = clean(section.heading.split(" / ").at(-1) ?? "");
  const localText = `${heading}\n${clean(section.text)}`;
  let lexical = 0; let matched = 0; let total = 0; let count = 0; let headingMatch = false;
  for (const [term, weight] of weights) {
    const inHeading = matches(heading, term);
    const weighted = weight * (inHeading ? HEADING_WEIGHT : 1);
    total += weight;
    if (!matches(text, term)) continue;
    matched += weight;
    if (matches(localText, term)) count += 1;
    headingMatch ||= inHeading;
    lexical += weighted;
  }
  const semantic = Math.max(0, ...hits.filter(hit => hit.pageId === section.page.id && Number.isFinite(hit.score)
    && overlaps(section.text, hit.text)).map(hit => hit.score));
  return { section, lexical, semantic, coverage: total ? matched / total : 0, matches: count, headingMatch };
}

/** Short topical questions can match one heading; broad body overlap needs multiple query terms. */
function lexicalMatch(candidate: Candidate, terms: number): boolean {
  return candidate.coverage >= MIN_QUERY_COVERAGE && (candidate.headingMatch || candidate.matches >= Math.min(2, terms));
}

/**
 * A strong semantic hit qualifies the current project's pages on its own. Another project's page never skips
 * the cross-project gate, because a high embedding score alone is how loosely related pages leaked in.
 */
function admitted(candidate: Candidate, terms: number, currentProjectId: string | undefined,
  semanticAvailable: boolean): boolean {
  const isLocal = !currentProjectId || belongsTo(candidate.section, currentProjectId);
  if (isLocal && candidate.semantic >= SEMANTIC_ONLY_MINIMUM) return true;
  if (!lexicalMatch(candidate, terms)) return false;
  if (isLocal) return true;
  if (candidate.matches < MIN_CROSS_PROJECT_TERMS) return false;
  return semanticAvailable ? candidate.semantic >= CROSS_PROJECT_SEMANTIC_MINIMUM
    : candidate.coverage >= MIN_CROSS_PROJECT_COVERAGE;
}

/** Pages without ownership metadata are treated as local so legacy pages keep their prior ranking. */
function belongsTo(section: DecisionSection, projectId: string): boolean {
  const sources = sourceProjectIds(section.page.frontmatter);
  return !sources.length || sources.includes(projectId);
}

/** Historical questions may still need an unlabelled rationale, so prefer rather than exclude. */
function historyPreference(section: DecisionSection, intent: string): number {
  return Number(intent === "historical" && section.temporalStatus === "historical");
}

/** Embedding hits must overlap the current section rather than merely point at its page. */
function overlaps(section: string, chunk: string): boolean {
  if (!chunk.trim()) return false;
  return section.split(/\n\s*\n/).some(paragraph => {
    const text = paragraph.trim();
    return text.length >= 12 && (chunk.includes(text) || text.includes(chunk.trim()));
  });
}
