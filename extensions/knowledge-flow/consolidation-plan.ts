/** Resolve reviewed planning proposals into stable scoped topic identities before editing prose. */
import { parseFrontmatter, slugify } from "../../src/utils/markdown.js";
import { sha256Text } from "../../src/connectors/hash.js";
import { sourceProjectIds } from "../../src/utils/topic-scope.js";
import type { FlowJob } from "./types.js";

export const MAX_TOPIC_CONTEXT_CHARS = 120_000;
/**
 * A full date (2026-09-24, 2026/9/24, 2026年9月24日) or a standalone month-day batch number (0918) names one action,
 * not a durable topic. A bare year is left alone because annual plans are durable.
 */
const ONE_OFF_LABEL = /(?<!\d)20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?!\d)|(?<![\p{L}\p{N}])(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?![\p{L}\p{N}])/u;

export interface TopicPlan {
  summary: string;
  disposition: "edit" | "noop" | "needs_review";
  reason: string;
  pages: Array<{ action: "create" | "update"; targetPageId: string | null; title: string;
    topic: string; decisionObject: string; reason: string }>;
}

export interface PlannedPage {
  pageId: string;
  topicId: string;
  title: string;
  topic: string;
  decisionObject: string;
  basisHash: string | null;
  original: string | null;
}

/** Include summaries of all scoped topics, not a file-name heuristic or only the current session's pages. */
export function topicCatalog(existing: ReadonlyMap<string, string>): unknown[] {
  return [...existing].map(([pageId, text]) => {
    const { meta, body } = parseFrontmatter(text);
    return { pageId, title: meta.title, sourceProjectIds: sourceProjectIds(meta),
      topic: meta.knowledgeTopic, decisionObject: meta.knowledgeDecisionObject,
      summary: meta.summary, headings: body.split("\n").filter(line => /^#{1,3} /.test(line)) };
  });
}

/** Bound the exact compact catalog plus full originals selected for semantic edits and review. */
export function assertTopicContextBudget(existing: ReadonlyMap<string, string>, selectedPageIds: readonly string[] = []): void {
  const catalogCharacters = JSON.stringify(topicCatalog(existing)).length;
  const selectedCharacters = [...new Set(selectedPageIds)].reduce((total, pageId) => total + (existing.get(pageId)?.length ?? 0), 0);
  if (catalogCharacters + selectedCharacters > MAX_TOPIC_CONTEXT_CHARS) {
    throw new Error(`semantic topic context exceeds ${MAX_TOPIC_CONTEXT_CHARS} characters`);
  }
}

/** Stable IDs survive synonym labels. A new destination requires a separate decision object. */
export function resolvePlan(plan: TopicPlan, job: FlowJob, existing: ReadonlyMap<string, string>): PlannedPage[] {
  if (plan.disposition !== "edit") {
    if (plan.pages.length) throw new Error("non-edit plan cannot contain pages");
    return [];
  }
  if (!plan.pages.length) throw new Error("edit plan has no destinations");
  const output = plan.pages.map(page => resolvePage(page, job, existing));
  if (new Set(output.map(page => page.pageId)).size !== output.length) throw new Error("duplicate topic destination");
  return output;
}

/** Only new pages are checked, so existing dated pages can still be updated until they are migrated. */
function assertDurableName(page: TopicPlan["pages"][number]): void {
  if ([page.title, page.decisionObject].some(value => ONE_OFF_LABEL.test(value))) {
    throw new Error("new topic is named after one dated action or batch; use a durable topic and record the action in its timeline section");
  }
}

function resolvePage(page: TopicPlan["pages"][number], job: FlowJob, existing: ReadonlyMap<string, string>): PlannedPage {
  if ([page.topic, page.title, page.decisionObject].some(value => /\p{Cf}/u.test(value))) throw new Error("topic labels contain invisible characters");
  if (page.action === "update") return existingPage(page, job, existing);
  if (page.targetPageId !== null) throw new Error("new topic cannot target an existing page");
  assertDurableName(page);
  const semantic = job.topicScope === "semantic";
  const same = [...existing.values()].some(body => {
    const { meta } = parseFrontmatter(body);
    return normalized(String(meta.knowledgeTopic ?? "")) === normalized(page.topic)
      && normalized(String(meta.knowledgeDecisionObject ?? "")) === normalized(page.decisionObject);
  });
  if (same) throw new Error("new topic duplicates an existing decision object");
  const identity = semantic ? ["semantic", normalized(page.topic), normalized(page.decisionObject)]
    : [job.projectId, normalized(page.topic), normalized(page.decisionObject)];
  const topicId = sha256Text(JSON.stringify(identity));
  // Concurrent readers must choose the same path even when display titles differ.
  const label = slugify(semantic ? normalized(page.topic) : `${job.projectLabel}-${page.title}`).slice(0, 100).replace(/-$/, "") || "topic";
  const pageId = `concepts/${label}-${topicId.slice(0, 8)}`;
  if (existing.has(pageId)) throw new Error("new topic page already exists");
  return { pageId, topicId, title: page.title, topic: page.topic, decisionObject: page.decisionObject, basisHash: null, original: null };
}

function existingPage(page: TopicPlan["pages"][number], job: FlowJob, existing: ReadonlyMap<string, string>): PlannedPage {
  const pageId = page.targetPageId;
  if (!pageId || !job.allowedPageIds.includes(pageId) || !existing.has(pageId)) throw new Error("topic outside project scope");
  const original = existing.get(pageId)!;
  const { meta } = parseFrontmatter(original);
  validateExistingPageScope(meta, job);
  // An update plan is the explicit user-facing correction for stale labels. Keep
  // the frozen page identity and basis, while review decides whether new labels
  // still describe the same business decision object.
  const topic = page.topic;
  const decisionObject = page.decisionObject;
  const topicId = typeof meta.knowledgeTopicId === "string" ? meta.knowledgeTopicId
    : sha256Text(JSON.stringify(job.topicScope === "semantic" ? ["semantic", pageId] : [job.projectId, pageId]));
  return { pageId, topicId, title: page.title, topic, decisionObject, basisHash: sha256Text(original), original };
}

function validateExistingPageScope(meta: Record<string, unknown>, job: FlowJob): void {
  if (job.topicScope === "semantic") return;
  if (meta.topicScope === "semantic") throw new Error("legacy job cannot update semantic topic");
  if (meta.projectId && meta.projectId !== job.projectId) throw new Error("topic project ownership mismatch");
}

function normalized(value: string): string { return value.normalize("NFC").trim().replace(/\s+/g, " ").toLowerCase(); }
