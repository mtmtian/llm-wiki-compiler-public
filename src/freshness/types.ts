/**
 * Types for the computed source-freshness layer.
 *
 * Freshness is derived on demand from the filesystem + state.json and is never
 * persisted. `FreshnessSnapshot` is built once per command/viewer snapshot and
 * shared by every consumer (lint, export, MCP, context, viewer).
 */

import type { StateStatus } from "../utils/state.js";

/** A page's computed freshness on the source-derived axis. */
export type FreshnessStatus = "fresh" | "stale" | "orphaned" | "unverified";

/** The minimal page shape `computeFreshness` needs; each surface adapts to it. */
export interface PageFreshnessInput {
  slug: string;
  /**
   * `"concepts"`/`"queries"` for a default page. Widened past that pair because
   * a TYPED entity page (a non-default profile's `articles/`, `papers/`, …) also
   * asks for freshness, and `classify` is already written for it: a typed page
   * owns no source entry, so it degrades to `unverified` through the documented
   * empty-owners path rather than needing a status invented for it.
   */
  pageDirectory: string;
  frontmatter: Record<string, unknown>;
}

/** One source's freshness inputs within a snapshot. */
export interface SourceFreshness {
  /** Hash recorded in state.json at last compile. */
  recordedHash: string;
  /** Hash of the source file on disk now, or null when the file is missing. */
  currentHash: string | null;
  /** Whether the source file still exists on disk. */
  exists: boolean;
  /** Concept slugs this source produced (state.json ownership map). */
  concepts: string[];
}

/** Reusable, per-run snapshot of all freshness inputs. */
export interface FreshnessSnapshot {
  stateStatus: StateStatus;
  /** Source filename -> its freshness inputs (empty unless stateStatus === "ok"). */
  sources: Record<string, SourceFreshness>;
}

/** The three orthogonal signals exposed per page. */
export interface PageFreshness {
  freshnessStatus: FreshnessStatus;
  contradicted: boolean;
  archived: boolean;
}
