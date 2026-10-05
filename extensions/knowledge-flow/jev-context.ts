/** Optional post-retrieval reranking. All unavailable/invalid trial paths return the original pack intact. */
import type { TaskContext } from "../../src/context/task-types.js";
import { JevError, jevDependencies, jevPayload, requestJev, type JevDependencies, type Scores } from "./jev-client.js";
import { NANO_PER_INPUT_TOKEN, reserveTrial, settleTrial, stopReason, trialStatus, type JevSettings } from "./jev-ledger.js";

export interface JevConfig { stateDir?: string; jevContext?: JevSettings }
export interface JevRankingResult { pack: TaskContext; status: { mode: "rules" | "jev"; reason: string } }
const rules = (pack: TaskContext, reason: string): JevRankingResult => ({ pack, status: { mode: "rules", reason } });

/** Never widen scope, synthesize evidence, or alter historical/source qualifiers. */
function rankedPack(pack: TaskContext, scores: Scores[]): TaskContext {
  const evidence = pack.evidence.map((item, index) => ({ item, index, score: scores[index] }))
    .filter(({ score }) => score.irrelevant < .9)
    .sort((a, b) => (2 * b.score.direct + b.score.supporting) - (2 * a.score.direct + a.score.supporting) || a.index - b.index)
    .map(({ item }) => item);
  const removed = evidence.length < pack.evidence.length;
  return { ...pack, evidence, complete: pack.complete && !removed,
    status: !evidence.length && pack.status === "ok" ? "no-hit" : pack.status,
    followUpPageIds: removed ? [...new Set([...pack.followUpPageIds, ...pack.evidence.flatMap(item => item.origin === "ledger" ? [] : [item.pageId])])] : pack.followUpPageIds,
    ...(pack.followUpClaimRefs || pack.evidence.some(item => item.origin === "ledger") ? { followUpClaimRefs: removed
      ? [...new Set([...(pack.followUpClaimRefs ?? []), ...pack.evidence.flatMap(item => item.origin === "ledger" ? [item.claimRef] : [])])]
      : pack.followUpClaimRefs } : {}) };
}

/** Externally callable seam: disabled/offline/exhausted behavior is independent of the transport. */
export async function rerankJev(pack: TaskContext, prompt: string, config: JevConfig,
  dependencies: JevDependencies = jevDependencies, timeout = 1800): Promise<JevRankingResult> {
  if (!config.jevContext?.enabled) return rules(pack, "disabled");
  if (pack.evidence.length < 2) return rules(pack, "no-rerank-needed");
  if (!config.stateDir) return rules(pack, "missing-state-directory");
  if (timeout < 200) return rules(pack, "time-budget");
  try { return await runTrial(pack, prompt, { stateDir: config.stateDir, jevContext: config.jevContext }, dependencies, timeout); }
  catch { return rules(pack, "state-unavailable"); }
}

/** Reserve only after input validation; conservative reservations survive crashes and unknown usage. */
async function runTrial(pack: TaskContext, prompt: string, config: JevConfig & { stateDir: string; jevContext: JevSettings },
  dependencies: JevDependencies, timeout: number) {
  const reason = stopReason(config.jevContext, trialStatus(config.stateDir));
  if (reason) return rules(pack, reason);
  const body = jevPayload(prompt, pack.evidence);
  if (Buffer.byteLength(body) > 24_000) return rules(pack, "input-budget");
  const start = Date.now();
  let key: string;
  try { key = await dependencies.key(); } catch { return rules(pack, "credential-unavailable"); }
  const remaining = Math.floor(timeout - (Date.now() - start));
  if (remaining < 200) return rules(pack, "time-budget");
  const reservation = reserveTrial(config.stateDir, config.jevContext, (Buffer.byteLength(body) + 2048) * NANO_PER_INPUT_TOKEN);
  if (!reservation.id) return rules(pack, reservation.reason);
  return scoreReserved(pack, body, key, remaining, config.stateDir, reservation.id, dependencies);
}

/** Do not let billing, transport, parsing or accounting failures remove useful baseline context. */
async function scoreReserved(pack: TaskContext, body: string, key: string, timeout: number,
  directory: string, reservation: string, dependencies: JevDependencies): Promise<JevRankingResult> {
  try {
    const result = await requestJev(body, key, timeout, dependencies.fetch, pack.evidence.length);
    settleTrial(directory, reservation, "", result.usage);
    return { pack: rankedPack(pack, result.scores), status: { mode: "jev", reason: "success" } };
  } catch (error) {
    const reason = error instanceof JevError ? error.message : "transport-failure";
    settleTrial(directory, reservation, reason);
    return rules(pack, reason);
  }
}
