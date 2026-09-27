/**
 * Apply an explicitly reviewed legacy grouping without changing its immutable
 * packets, conclusions or evidence. The Python replica pins the shared manifest;
 * this process boundary revalidates the exact refs and project ownership.
 */
import { z } from "zod";
import type { PublicationEntry } from "./publication-types.js";
import type { TopicRouteGroup } from "./types.js";

const text = z.string().trim().min(1).max(160);
const groupSchema = z.object({ projectId: text, topic: text, decisionObject: text,
  claimRefs: z.array(z.string().regex(/^[a-f0-9]{64}:[0-4]$/)).min(1).max(5000) }).strict();

/** Order has no meaning in a reviewed routing snapshot. */
export function canonicalTopicRoutes(value: unknown): TopicRouteGroup[] {
  const parsed = z.array(groupSchema).max(1000).safeParse(value);
  if (!parsed.success) throw new Error("invalid topic routes");
  const groups = parsed.data.map(group => ({ ...group, claimRefs: [...group.claimRefs].sort() }));
  if (groups.reduce((count, group) => count + group.claimRefs.length, 0) > 5000) throw new Error("too many topic route refs");
  return groups.sort((left, right) => left.claimRefs[0] < right.claimRefs[0] ? -1 : left.claimRefs[0] > right.claimRefs[0] ? 1 : 0);
}

/** Route metadata is separate from the original record; unlisted claims remain unchanged. */
export function routePublicationEntries(entries: PublicationEntry[], routes: TopicRouteGroup[]): PublicationEntry[] {
  const original = new Map(entries.map(entry => [entry.ref, entry]));
  const routed = new Map<string, PublicationEntry>();
  for (const group of canonicalTopicRoutes(routes)) {
    const routingGroup = JSON.stringify(group.claimRefs);
    for (const ref of group.claimRefs) {
      const entry = original.get(ref);
      if (!entry || routed.has(ref) || entry.record.payload.projectId !== group.projectId || entry.claim.decisionObject) {
        throw new Error("topic route must identify a unique legacy claim in its original project");
      }
      routed.set(ref, { ...entry, routingGroup, claim: { ...entry.claim, topic: group.topic,
        decisionObject: group.decisionObject, targetPageId: null } });
    }
  }
  return entries.map(entry => routed.get(entry.ref) ?? entry);
}
