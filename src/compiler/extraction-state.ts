/**
 * Source-state persistence for concept extraction.
 *
 * Keeps the orchestration spine focused on pipeline ordering while preserving
 * the distinction between live concepts, explicit empty success, and failed
 * extraction output.
 */

import { hashContent } from "./hasher.js";
import { slugify } from "../utils/markdown.js";
import type { SourceState } from "../utils/types.js";
import type { CompileStateDraft } from "./compile-state-draft.js";
import { extractionSucceeded, type ExtractionResult } from "./deps.js";
import type { MergedConcept } from "./types.js";

/** Persist every successful extraction's source cursor and live ownership. */
export function persistExtractionStates(
  draft: CompileStateDraft,
  extractions: ExtractionResult[],
  writtenPages: MergedConcept[],
  retained: ReadonlySet<string> = new Set(),
): void {
  const liveSlugsForSource = buildLiveSlugsForSource(writtenPages);
  for (const result of extractions) {
    if (!extractionSucceeded(result)) continue;
    if (result.concepts.length === 0) {
      persistEmptyExtractionState(draft, result);
      continue;
    }
    const liveSlugs = new Set(liveSlugsForSource.get(result.sourceFile) ?? []);
    addRetainedSlugs(result, retained, liveSlugs);
    persistSourceStateFiltered(draft, result, liveSlugs);
  }
}

/** Keep ownership of concepts awaiting reconciliation or a retry. */
function addRetainedSlugs(
  result: ExtractionResult,
  retained: ReadonlySet<string>,
  liveSlugs: Set<string>,
): void {
  for (const concept of result.concepts) {
    const slug = slugify(concept.concept);
    if (retained.has(slug)) liveSlugs.add(slug);
  }
}

/** Persist an explicit empty extraction unless an existing owner needs review. */
function persistEmptyExtractionState(
  draft: CompileStateDraft,
  result: ExtractionResult,
): void {
  if (result.needsReview) return;
  draft.setSource(result.sourceFile, {
    hash: hashContent(result.sourceContent),
    concepts: [],
    compiledAt: new Date().toISOString(),
  });
}

/** Build a map from source filename to the slugs written live this run. */
function buildLiveSlugsForSource(writtenPages: MergedConcept[]): Map<string, string[]> {
  const map = new Map<string, string[]>();
  for (const page of writtenPages) {
    for (const sourceFile of page.sourceFiles) {
      const existing = map.get(sourceFile) ?? [];
      existing.push(page.slug);
      map.set(sourceFile, existing);
    }
  }
  return map;
}

/** Persist source state, filtering concepts to only those in the live set. */
function persistSourceStateFiltered(
  draft: CompileStateDraft,
  result: ExtractionResult,
  liveSlugs: Set<string>,
): void {
  const entry: SourceState = {
    hash: hashContent(result.sourceContent),
    concepts: result.concepts
      .map((c) => slugify(c.concept))
      .filter((s) => liveSlugs.has(s)),
    compiledAt: new Date().toISOString(),
  };
  draft.setSource(result.sourceFile, entry);
}
