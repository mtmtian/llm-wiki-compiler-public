/** Canonical project provenance for semantic pages and legacy project-owned pages. */

/** Combine semantic provenance with legacy ownership and return stable unique IDs. */
export function sourceProjectIds(meta: Record<string, unknown>): string[] {
  const values = Array.isArray(meta.sourceProjectIds) ? meta.sourceProjectIds : [];
  const ids = values.filter(isProjectId).map(value => value.trim());
  if (isProjectId(meta.projectId)) ids.push(meta.projectId.trim());
  return [...new Set(ids)].sort((left, right) => left.localeCompare(right));
}

/** Accept non-empty string project identifiers while preserving their stored spelling. */
function isProjectId(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
