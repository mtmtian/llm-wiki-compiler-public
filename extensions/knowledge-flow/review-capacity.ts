/**
 * Count unresolved review items across interrupted retry handoffs. Explicit,
 * same-project, linear chains share a slot; malformed or branching records
 * retain their individual slots until an operator resolves their provenance.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

interface ReviewRecord { id: string; jobId?: unknown; projectId?: unknown; reviewRetryOf?: unknown; }

/** Return logical pending slots without editing any historical review records. */
export async function pendingReviewCount(stateDir: string, projectId: string, replacedId?: string): Promise<number> {
  const folder = path.join(stateDir, "review");
  const names = await readdir(folder).catch((error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return [];
    throw error;
  });
  const loaded = await Promise.all(names.filter(name => name.endsWith(".json")).map(async name => {
    try { return { ...JSON.parse(await readFile(path.join(folder, name), "utf8")), id: name.slice(0, -5) } as ReviewRecord; }
    catch { return null; }
  }));
  const records = loaded.filter((record): record is ReviewRecord => record?.projectId === projectId);
  return reviewGroups(records).reduce((count, group) => count + Number(replacedId === undefined || !group.includes(replacedId)), 0);
}

/** A linear retry lineage shares one slot; every record in an ambiguous component keeps its own. */
function reviewGroups(records: ReviewRecord[]): string[][] {
  const parents = reviewParents(records);
  const pending = new Set(records.map(record => record.id));
  const groups: string[][] = [];
  while (pending.size) {
    const component = connected(pending.values().next().value!, parents);
    component.forEach(id => pending.delete(id));
    const edges = [...parents].filter(([id]) => component.has(id));
    const linear = edges.length === component.size - 1 && new Set(edges.map(([, parent]) => parent)).size === edges.length;
    groups.push(...(linear ? [[...component]] : [...component].map(id => [id])));
  }
  return groups;
}

/** Invalid identities never hide another review; self-links remain cycles rather than disappearing. */
function reviewParents(records: readonly ReviewRecord[]): Map<string, string> {
  const byId = new Map(records.map(record => [record.id, record]));
  const parents = new Map<string, string>();
  for (const record of records) {
    const parent = typeof record.reviewRetryOf === "string" ? byId.get(record.reviewRetryOf) : undefined;
    if (parent && record.jobId === record.id && validIdentity(parent)) parents.set(record.id, parent.id);
  }
  return parents;
}

/** Older parent records may omit their ID; an explicit different or null ID is invalid. */
function validIdentity(record: ReviewRecord): boolean {
  return record.jobId === undefined || record.jobId === record.id;
}

/** Traverse only explicit links whose two endpoints were validated in the same project. */
function connected(start: string, parents: ReadonlyMap<string, string>): Set<string> {
  const result = new Set([start]);
  for (const id of result) {
    for (const [child, parent] of parents) {
      if (child === id) result.add(parent);
      if (parent === id) result.add(child);
    }
  }
  return result;
}
