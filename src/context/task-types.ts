/** Query-specific, project-scoped evidence shared by native hooks and MCP. */
import type { ContextPrimary } from "./types.js";
import type { SectionTemporalStatus } from "./task-temporal.js";

export interface TaskContextOptions {
  root: string;
  prompt: string;
  projectId?: string;
  scope?: "project" | "semantic";
  allowedPageIds?: string[];
}

export interface TaskEvidence {
  pageId: string;
  title: string;
  pageRevision: string;
  updatedAt: string | null;
  decisionObject: string | null;
  section: string;
  text: string;
  qualifications: string;
  /** Explicit page/heading label, not a verified effective date or live implementation state. */
  temporalStatus?: SectionTemporalStatus;
  sources: ContextPrimary["sourceWindows"];
  sourceProjectIds?: string[];
}

export interface TaskContext {
  version: 1;
  projectId: string | null;
  status: "ok" | "no-hit" | "degraded" | "ambiguous-scope";
  evidence: TaskEvidence[];
  complete: boolean;
  followUpPageIds: string[];
  diagnostics: { scopedPages: number; matchedSections: number; warnings: string[] };
}
